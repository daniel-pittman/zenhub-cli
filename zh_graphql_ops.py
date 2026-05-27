"""ZenHub GraphQL operations: sub-issue queries/mutations + sprint queries.

This module is the direct-Python replacement for the bash ↔ MCP text
contract that v1.5.0 used. Each function takes a resolved RepoContext
(see zh_api.py) and returns native Python data structures sourced from
the ZenHub GraphQL response — no text-parsing layer, no truncation, no
em-dash sentinels, no `RESULT:` lines.

Logic-level findings carried forward from the v1.5.0 release review:

  - Pagination stuck-cursor + iteration-cap defense (zenhub_child_issues
    walker)
  - Cross-repo case-insensitive owner/repo comparison (see zh_api.repos_match)
  - Mid-loop validation accumulator on remove (don't drop accumulated
    mismatches at the first error)
  - Numeric format validation up-front (clean ZhApiError, not raw
    GraphQL crash from a non-integer issue number)

A note on the `addSubIssues` re-parent question (issue #10):

The ZenHub API exposes `AddSubIssuesInput.replaceParent: Boolean`. With
`replaceParent` unset/false (our default), a child that already has a
different parent is REJECTED by the API and surfaces in
`AddSubIssuesPayload.failedIssues` — it is NOT silently re-parented. So
this module deliberately does NOT pass replaceParent and does NOT do a
symmetric wrong-parent pre-flight on `add` (the API's failedIssues array
is the authoritative report). On `remove` the parent is fed by us and
the API would unlink a child from its actual parent if we lied about
the parent number, so we keep the pre-flight there.
"""

from __future__ import annotations

# The MCP server adds this file's directory to sys.path before importing
# this module, so a flat sibling import works in both the MCP runtime and
# the pytest harness (which uses an explicit sys.path tweak in conftest.py).
from zh_api import (
    RepoContext,
    ZhApiError,
    check_graphql_errors,
    repos_match,
    _ISSUE_BY_INFO_QUERY,
)


def _is_positive_int(n) -> bool:  # noqa: ANN001
    """Strict positive-int check that rejects bool.

    `isinstance(n, int)` returns True for `bool` (bool is a subclass
    of int in Python), so naive validation `isinstance(n, int) and
    n > 0` would accept `True` (== 1) and `False` (== 0). This
    helper rejects bools explicitly. Round-5 #10.
    """
    return (
        isinstance(n, int)
        and not isinstance(n, bool)
        and n > 0
    )


def get_issue_by_info(
    ctx: RepoContext, issue_number: int
) -> dict | None:
    """Fetch a single issue's structural info via the context's query method.

    Returns None when the issue doesn't exist in this repository. Raises
    ZhApiError for bad input or top-level GraphQL errors.
    """
    if not _is_positive_int(issue_number):
        raise ZhApiError(
            f"issue number must be a positive int (got {issue_number!r})"
        )
    resp = ctx.query(
        _ISSUE_BY_INFO_QUERY,
        {"repoId": ctx.repo_id, "issueNumber": issue_number},
    )
    check_graphql_errors(resp, context="issueByInfo")
    return (resp.get("data") or {}).get("issueByInfo")


# =============================================================================
# Pagination defenses
# =============================================================================

MAX_PAGINATION_ITERATIONS = 200
"""Hard cap on `pageInfo.hasNextPage` walks before bailing.

Belt-and-suspenders backstop in case the cursor-equality check also
fails (e.g. the API alternates between two cursors). At 100 items per
page that's 20k items — well past any sensible parent's children count.
"""


# =============================================================================
# Sub-issue: LIST
# =============================================================================

_SUBISSUE_LIST_QUERY = """
query($repoId: ID!, $issueNumber: Int!, $workspaceId: ID!, $after: String) {
  issueByInfo(repositoryId: $repoId, issueNumber: $issueNumber) {
    number
    title
    state
    zenhubChildIssues(first: 100, after: $after) {
      totalCount
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        number
        title
        state
        assignees {
          nodes { login }
        }
        pipelineIssue(workspaceId: $workspaceId) {
          pipeline { name }
        }
        pipelineIssues {
          nodes { pipeline { name } }
        }
        repository {
          ownerName
          name
        }
      }
    }
  }
}
"""


def list_sub_issues(ctx: RepoContext, parent_number: int) -> dict:
    """List every sub-issue of a parent, walking pagination.

    Returns:
        dict with keys:
            ok: bool
            parent_number: int
            parent_title: str
            parent_state: str | None
            total_count: int — API's zenhubChildIssues.totalCount
            fetched_count: int — what we actually walked
            children: list[dict] with
                id: str — workspace-global GraphQL gid
                number: int
                title: str (untruncated)
                state: "OPEN" | "CLOSED"
                pipeline: str | None — pipeline name for the child.
                    Sourced from `pipelineIssue(workspaceId=ctx.workspace_id)`
                    first; falls back to `pipelineIssues.nodes[0]` for
                    children that live in a different workspace.
                pipeline_workspace_scoped: bool — True if `pipeline` is
                    from the ctx workspace (authoritative for sprint
                    planning), False if from the fallback (informational
                    only — child lives in another workspace).
                assignees: list[str]
                repository: {"owner": str, "name": str}
            pagination_warning: str | None — set if we bailed defensively
    """
    if not _is_positive_int(parent_number):
        raise ZhApiError(
            f"parent_number must be a positive int (got {parent_number!r})"
        )

    children: list[dict] = []
    parent_title = ""
    parent_state: str | None = None
    total_count = 0
    pagination_warning: str | None = None

    cursor: str | None = None
    last_cursor: str | None = None
    iterations = 0
    first_page = True

    while True:
        iterations += 1
        if iterations > MAX_PAGINATION_ITERATIONS:
            pagination_warning = (
                f"Pagination iteration cap ({MAX_PAGINATION_ITERATIONS}) "
                "exceeded — bailing"
            )
            break

        resp = ctx.query(
            _SUBISSUE_LIST_QUERY,
            {
                "repoId": ctx.repo_id,
                "issueNumber": parent_number,
                "workspaceId": ctx.workspace_id,
                "after": cursor,
            },
        )
        check_graphql_errors(resp, context="list_sub_issues")
        issue = (resp.get("data") or {}).get("issueByInfo")
        if not issue:
            return {
                "ok": False,
                "parent_number": parent_number,
                "parent_title": "",
                "parent_state": None,
                "total_count": 0,
                "fetched_count": 0,
                "children": [],
                "pagination_warning": None,
                "error": f"Issue #{parent_number} not found in this repository",
            }

        if first_page:
            parent_title = issue.get("title") or ""
            parent_state = issue.get("state")
            total_count = (
                (issue.get("zenhubChildIssues") or {}).get("totalCount") or 0
            )
            first_page = False

        conn = issue.get("zenhubChildIssues") or {}
        for node in conn.get("nodes") or []:
            # Defensive normalization: skip a None entry rather than
            # crashing on `node.get(...)`. The API contract is a non-
            # nullable list of nodes, but matrix tests pin against
            # the surprise shape.
            if node is None:
                continue
            assignees = [
                a.get("login")
                for a in ((node.get("assignees") or {}).get("nodes") or [])
                if a.get("login")
            ]
            # Review finding #7: `pipelineIssue(workspaceId:...)` returns
            # null when the child lives in a workspace OTHER than the
            # one ctx.workspace_id targets (typical in cross-repo
            # workspaces). Fall back to `pipelineIssues.nodes[0]` (no
            # workspace arg) so cross-workspace children still surface
            # *a* pipeline name — flagged via `pipeline_workspace_scoped`
            # so callers know whether the value is for the current
            # workspace or some other one.
            pipeline_name = None
            pipeline_workspace_scoped = False
            pi = node.get("pipelineIssue") or None
            if pi:
                pl = pi.get("pipeline") or {}
                pipeline_name = pl.get("name") or None
                if pipeline_name:
                    pipeline_workspace_scoped = True
            if not pipeline_name:
                fallback_nodes = (
                    (node.get("pipelineIssues") or {}).get("nodes") or []
                )
                if fallback_nodes:
                    fp = (fallback_nodes[0] or {}).get("pipeline") or {}
                    pipeline_name = fp.get("name") or None

            repo = node.get("repository") or {}
            children.append({
                # `id` is the workspace-global GraphQL gid. Mutations
                # (e.g. reprioritizeSubIssue's afterId/beforeId) take
                # gids, so emitting it here eliminates a second
                # round-trip via get_issue_by_info when anchoring.
                # Review finding #2(a).
                "id": node.get("id"),
                "number": node.get("number"),
                "title": node.get("title") or "",
                "state": node.get("state") or "UNKNOWN",
                "pipeline": pipeline_name,
                "pipeline_workspace_scoped": pipeline_workspace_scoped,
                "assignees": assignees,
                "repository": {
                    "owner": repo.get("ownerName") or "",
                    "name": repo.get("name") or "",
                },
            })

        page_info = conn.get("pageInfo") or {}
        has_next = bool(page_info.get("hasNextPage"))
        end_cursor = page_info.get("endCursor")
        if not has_next:
            break
        # Stuck-cursor defense. Three bad cases all collapse to "bail":
        #   - end_cursor is None despite hasNextPage=true (server bug;
        #     would loop forever with cursor=None re-requesting page 1)
        #   - end_cursor is the empty string (same defect)
        #   - end_cursor equals the cursor we just used (cursor isn't
        #     advancing; server returning the same page)
        # The first two cases used to be silently bypassed by the
        # `if end_cursor and ...` short-circuit — they now trip the
        # warning explicitly. Review finding #11.
        if not end_cursor or end_cursor == last_cursor:
            pagination_warning = (
                "Pagination cursor not advancing across requests — "
                "server likely mis-reporting hasNextPage. Bailing."
            )
            break
        last_cursor = end_cursor
        cursor = end_cursor

    return {
        "ok": True,
        "parent_number": parent_number,
        "parent_title": parent_title,
        "parent_state": parent_state,
        "total_count": total_count,
        "fetched_count": len(children),
        "children": children,
        "pagination_warning": pagination_warning,
    }


# =============================================================================
# Sub-issue: ADD / REMOVE
# =============================================================================

_ADD_SUB_ISSUES_MUTATION = """
mutation($input: AddSubIssuesInput!) {
  addSubIssues(input: $input) {
    successCount
    failedIssues {
      number
      repository { ownerName name }
    }
    githubErrors
  }
}
"""

_REMOVE_SUB_ISSUES_MUTATION = """
mutation($input: RemoveSubIssuesInput!) {
  removeSubIssues(input: $input) {
    successCount
    failedIssues {
      number
      repository { ownerName name }
    }
    githubErrors
  }
}
"""


def _resolve_child_id(
    ctx: RepoContext, child_number: int
) -> dict:
    """Look up a child issue's GraphQL id + its current parent (if any)."""
    if not _is_positive_int(child_number):
        raise ZhApiError(
            f"child issue number must be a positive int (got {child_number!r})"
        )
    issue = get_issue_by_info(ctx, child_number)
    if not issue:
        return {"ok": False, "child_number": child_number, "error": "not found"}
    return {"ok": True, "child_number": child_number, "issue": issue}


def _classify_outcome(success_count: int, failed_count: int) -> str:
    """Map (success, failed) counts to outcome keyword."""
    if failed_count > 0 and success_count > 0:
        return "partial"
    if failed_count > 0:
        return "fail"
    if success_count == 0:
        return "noop"
    return "ok"


def add_sub_issues(
    ctx: RepoContext, parent_number: int, child_numbers: list[int]
) -> dict:
    """Add child issues as sub-issues of `parent_number`.

    The ZenHub API's `AddSubIssuesInput.replaceParent` defaults to false:
    children already attached to a different parent appear in
    `failedIssues`. We rely on that rather than doing a symmetric
    pre-flight check (see module docstring).

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            parent_number: int
            outcome: "ok"|"partial"|"fail"|"noop"
            success_count: int
            failed_count: int
            succeeded: list[int] — child numbers the API actually linked
            failed: list[dict] — [{number, owner, name}, ...] for rejects
            github_errors: dict|None
            error: str|None — parent-not-found or other fatal cases

    Duplicate input numbers are collapsed first-occurrence
    (e.g. `[100, 100, 101]` is treated as `[100, 101]`) so the
    success accounting cannot be inflated by repeating an input.
    Mirrors the sprint mutations' boundary dedup (round-2 #10).
    """
    if not child_numbers:
        raise ZhApiError("child_numbers must be non-empty")
    for n in child_numbers:
        if not _is_positive_int(n):
            raise ZhApiError(
                f"every child number must be a positive int (got {n!r})"
            )

    # Boundary dedup — preserves first-occurrence order so the output
    # `succeeded` / `failed` lists are stable across calls.
    child_numbers = list(dict.fromkeys(child_numbers))

    parent_issue = get_issue_by_info(ctx, parent_number)
    if not parent_issue:
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "github_errors": None,
            "error": f"Parent #{parent_number} not found in this repository",
        }
    parent_id = parent_issue["id"]

    # Resolve each child's ID. The bash version did this in a single mid-
    # loop accumulator; we replicate the same pattern — collect every
    # not-found before bailing rather than failing at the first error.
    child_ids: list[str] = []
    not_found: list[int] = []
    for n in child_numbers:
        info = _resolve_child_id(ctx, n)
        if not info["ok"]:
            not_found.append(n)
        else:
            child_ids.append(info["issue"]["id"])
    if not_found:
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": len(not_found),
            "succeeded": [],
            "failed": [
                {"number": n, "owner": "", "name": ""} for n in not_found
            ],
            "github_errors": None,
            "error": (
                "Some child issue numbers were not found in this repository: "
                + ", ".join(f"#{n}" for n in not_found)
            ),
        }

    resp = ctx.query(
        _ADD_SUB_ISSUES_MUTATION,
        {"input": {"parentId": parent_id, "childIssueIds": child_ids}},
    )
    check_graphql_errors(resp, context="addSubIssues")
    payload = (resp.get("data") or {}).get("addSubIssues") or {}
    success_count = int(payload.get("successCount") or 0)
    failed_issues = payload.get("failedIssues") or []
    failed_count = len(failed_issues)
    github_errors = payload.get("githubErrors") or None
    if isinstance(github_errors, dict) and not github_errors:
        github_errors = None

    failed_serialized = [
        {
            "number": (fi.get("number") if isinstance(fi, dict) else None),
            "owner": (
                (fi.get("repository") or {}).get("ownerName") or ""
                if isinstance(fi, dict)
                else ""
            ),
            "name": (
                (fi.get("repository") or {}).get("name") or ""
                if isinstance(fi, dict)
                else ""
            ),
        }
        for fi in failed_issues
    ]
    failed_numbers = {
        fi["number"] for fi in failed_serialized if fi.get("number") is not None
    }
    inferred_succeeded = [n for n in child_numbers if n not in failed_numbers]
    # Review finding #3: the API exposes a count but not an array of
    # succeeded numbers. We INFER `succeeded` as "input minus failed",
    # but that inference is only safe when the inferred set's length
    # equals success_count. When it doesn't (success_count < inferred,
    # e.g. successCount=1 with no failedIssues but 3 inputs), we don't
    # know which inputs actually landed — return an empty `succeeded`
    # set with a partial_success_warning rather than silently claiming
    # all 3.
    partial_success_warning: str | None = None
    if success_count == len(inferred_succeeded):
        succeeded = inferred_succeeded
    else:
        succeeded = []
        partial_success_warning = (
            f"API returned successCount={success_count} but "
            f"failedIssues={failed_count} for {len(child_numbers)} inputs; "
            "cannot identify which inputs succeeded. Re-list the parent's "
            "children to determine actual state."
        )

    outcome = _classify_outcome(success_count, failed_count)
    # Round-6 #3: round-5 fixed the DATA (succeeded=[] when we can't
    # identify) but left the SIGNAL stale — `outcome` was still
    # computed from `success_count` alone, which produces "ok" for a
    # single-success-with-divergence case. `ok = outcome == "ok"`
    # then reported True even though `partial_success_warning` was
    # set. Force partial when the divergence guard fires, so signal
    # and data agree.
    #
    # Guard: only override when outcome was "ok". `noop` (success=0,
    # failed=0) has its own specific semantics — the divergence
    # check inferred succeeded=[input] but the API said success=0,
    # which IS a divergence by the strict length check; however,
    # the load-bearing signal there is "noop", not "partial". A
    # "fail" outcome (failed_count>0, success_count=0) also stays
    # untouched. Round-6 #3 only fixes the case where the API
    # claims success but we can't identify which inputs landed.
    if partial_success_warning is not None and outcome == "ok":
        outcome = "partial"
    return {
        "ok": outcome == "ok",
        "parent_number": parent_number,
        "outcome": outcome,
        "success_count": success_count,
        "failed_count": failed_count,
        "succeeded": succeeded,
        "failed": failed_serialized,
        "github_errors": github_errors,
        "partial_success_warning": partial_success_warning,
        "error": None,
    }


def remove_sub_issues(
    ctx: RepoContext, parent_number: int, child_numbers: list[int]
) -> dict:
    """Unlink each child from its parent.

    The API's `removeSubIssues` only takes child IDs — it unlinks each
    from its actual parent. We do a pre-flight check that each child
    actually has `parent_number` as its parent in the cwd's repo, to
    catch wrong-parent typos.

    Returns the same shape as `add_sub_issues` with `succeeded` listing
    the children actually unlinked.

    Duplicate input numbers are collapsed first-occurrence (matches
    `add_sub_issues` and the sprint mutations).
    """
    if not child_numbers:
        raise ZhApiError("child_numbers must be non-empty")
    for n in child_numbers:
        if not _is_positive_int(n):
            raise ZhApiError(
                f"every child number must be a positive int (got {n!r})"
            )

    # Boundary dedup.
    child_numbers = list(dict.fromkeys(child_numbers))

    parent_issue = get_issue_by_info(ctx, parent_number)
    if not parent_issue:
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "github_errors": None,
            "error": f"Parent #{parent_number} not found in this repository",
        }
    parent_id = parent_issue["id"]

    # Pre-flight: validate each child belongs to parent_number AND lives
    # in the current repo. Accumulate mismatches across the loop rather
    # than bailing at the first one (carried-forward finding).
    resolved: list[dict] = []
    not_found: list[int] = []
    wrong_parent: list[dict] = []
    cross_repo: list[dict] = []

    for n in child_numbers:
        info = _resolve_child_id(ctx, n)
        if not info["ok"]:
            not_found.append(n)
            continue
        issue = info["issue"]
        if not repos_match(issue.get("repository"), ctx.owner_repo):
            cross_repo.append(
                {
                    "number": n,
                    "owner": (issue.get("repository") or {}).get("ownerName") or "",
                    "name": (issue.get("repository") or {}).get("name") or "",
                }
            )
            continue
        actual_parent = issue.get("parentIssue") or None
        if not actual_parent or actual_parent.get("number") != parent_number:
            wrong_parent.append(
                {
                    "number": n,
                    "actual_parent": (
                        actual_parent.get("number") if actual_parent else None
                    ),
                }
            )
            continue
        resolved.append(issue)

    if not_found or wrong_parent or cross_repo:
        msgs: list[str] = []
        if not_found:
            msgs.append(
                "not found: " + ", ".join(f"#{n}" for n in not_found)
            )
        if cross_repo:
            msgs.append(
                "cross-repo: "
                + ", ".join(
                    f"#{c['number']}→{c['owner']}/{c['name']}" for c in cross_repo
                )
            )
        if wrong_parent:
            msgs.append(
                "wrong parent: "
                + ", ".join(
                    f"#{w['number']} (actual parent: "
                    f"{'#'+str(w['actual_parent']) if w['actual_parent'] else 'none'})"
                    for w in wrong_parent
                )
            )
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": (
                len(not_found) + len(wrong_parent) + len(cross_repo)
            ),
            "succeeded": [],
            "failed": [
                *[{"number": n, "owner": "", "name": ""} for n in not_found],
                *cross_repo,
                *[
                    {"number": w["number"], "owner": "", "name": ""}
                    for w in wrong_parent
                ],
            ],
            "github_errors": None,
            "error": "Pre-flight validation failed: " + "; ".join(msgs),
        }

    child_ids = [issue["id"] for issue in resolved]

    resp = ctx.query(
        _REMOVE_SUB_ISSUES_MUTATION,
        {"input": {"parentId": parent_id, "childIssueIds": child_ids}},
    )
    check_graphql_errors(resp, context="removeSubIssues")
    payload = (resp.get("data") or {}).get("removeSubIssues") or {}
    success_count = int(payload.get("successCount") or 0)
    failed_issues = payload.get("failedIssues") or []
    failed_count = len(failed_issues)
    github_errors = payload.get("githubErrors") or None
    if isinstance(github_errors, dict) and not github_errors:
        github_errors = None

    failed_serialized = [
        {
            "number": (fi.get("number") if isinstance(fi, dict) else None),
            "owner": (
                (fi.get("repository") or {}).get("ownerName") or ""
                if isinstance(fi, dict)
                else ""
            ),
            "name": (
                (fi.get("repository") or {}).get("name") or ""
                if isinstance(fi, dict)
                else ""
            ),
        }
        for fi in failed_issues
    ]
    failed_numbers = {
        fi["number"] for fi in failed_serialized if fi.get("number") is not None
    }
    inferred_succeeded = [n for n in child_numbers if n not in failed_numbers]
    # Same review-finding-#3 logic as add_sub_issues: only trust the
    # inferred set when its length matches successCount.
    partial_success_warning: str | None = None
    if success_count == len(inferred_succeeded):
        succeeded = inferred_succeeded
    else:
        succeeded = []
        partial_success_warning = (
            f"API returned successCount={success_count} but "
            f"failedIssues={failed_count} for {len(child_numbers)} inputs; "
            "cannot identify which inputs succeeded. Re-list the parent's "
            "children to determine actual state."
        )

    outcome = _classify_outcome(success_count, failed_count)
    # Round-6 #3: force partial when divergence guard fires. See
    # add_sub_issues for the rationale — signal must match data.
    if partial_success_warning is not None:
        outcome = "partial"
    return {
        "ok": outcome == "ok",
        "parent_number": parent_number,
        "outcome": outcome,
        "success_count": success_count,
        "failed_count": failed_count,
        "succeeded": succeeded,
        "failed": failed_serialized,
        "github_errors": github_errors,
        "partial_success_warning": partial_success_warning,
        "error": None,
    }


# =============================================================================
# Sub-issue: REORDER
# =============================================================================

_REPRIORITIZE_SUB_ISSUE_MUTATION = """
mutation($input: ReprioritizeSubIssueInput!) {
  reprioritizeSubIssue(input: $input) {
    success
    githubErrors
  }
}
"""


def reorder_sub_issue(
    ctx: RepoContext,
    child_number: int,
    position: str,
    sibling_number: int | None = None,
) -> dict:
    """Reorder a sub-issue among its parent's children.

    Position keywords:
      - "top" / "first"   — first sibling
      - "bottom" / "last" — last sibling
      - "after"           — requires sibling_number; right after sibling
      - "before"          — requires sibling_number; right before sibling

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            child_number: int
            parent_number: int | None
            position: str — normalized human-readable
            outcome: "ok"|"noop"|"fail"
            error: str | None
    """
    pos = (position or "").lower().strip()
    if pos in {"top", "first"}:
        pos = "top"
    elif pos in {"bottom", "last"}:
        pos = "bottom"
    elif pos in {"after", "before"}:
        if not _is_positive_int(sibling_number):
            raise ZhApiError(
                f"position {pos!r} requires a positive sibling_number "
                f"(got {sibling_number!r})"
            )
    else:
        raise ZhApiError(
            f"position must be one of top/bottom/after/before (got {position!r})"
        )

    # Resolve the child + its parent.
    if not _is_positive_int(child_number):
        raise ZhApiError(
            f"child_number must be a positive int (got {child_number!r})"
        )
    # Review finding #9: self-anchoring is meaningless (reorder relative
    # to yourself is a no-op or an API rejection). Catch it before
    # firing the mutation.
    if pos in {"after", "before"} and sibling_number == child_number:
        raise ZhApiError(
            f"cannot anchor #{child_number} {pos} itself (self-anchor)"
        )
    child_issue = get_issue_by_info(ctx, child_number)
    if not child_issue:
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": None,
            "position": pos,
            "outcome": "fail",
            "error": f"Child #{child_number} not found in this repository",
        }
    parent_info = child_issue.get("parentIssue") or None
    if not parent_info:
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": None,
            "position": pos,
            "outcome": "fail",
            "error": f"#{child_number} is not currently a sub-issue of any parent",
        }
    parent_number = parent_info.get("number")
    parent_id = parent_info.get("id")
    child_id = child_issue.get("id")

    # List siblings to anchor against.
    sibling_listing = list_sub_issues(ctx, parent_number)
    if not sibling_listing.get("ok"):
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": parent_number,
            "position": pos,
            "outcome": "fail",
            "error": sibling_listing.get("error") or "Could not list siblings",
        }
    siblings = sibling_listing.get("children") or []
    sibling_pagination_warning = sibling_listing.get("pagination_warning")

    # Round-6 #5: when the sibling listing bailed mid-walk on stuck
    # cursor / iteration cap, top/bottom anchoring is unsafe — the
    # "first" or "last" sibling in a PARTIAL set isn't the
    # workspace-global first/last, so the mutation would silently
    # corrupt sub-issue order. Refuse the mutation and surface the
    # pagination warning in the error.
    #
    # `after` / `before` with an explicit sibling_number stay valid:
    # the user named a specific sibling and we either find it in
    # the partial set (anchor is correct regardless of pagination
    # state) or fail with anchor-not-found (correct error). The
    # asymmetry is deliberate.
    if sibling_pagination_warning and pos in {"top", "bottom"}:
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": parent_number,
            "position": pos,
            "outcome": "fail",
            "error": (
                f"Cannot determine sibling order for '{pos}' under "
                f"partial pagination — sub-issue listing bailed: "
                f"{sibling_pagination_warning}. Re-list "
                f"('zh subissue list #{parent_number}') and retry "
                f"once full coverage is available, OR use 'after' / "
                f"'before' with an explicit sibling number."
            ),
        }

    after_id: str | None = None
    before_id: str | None = None
    position_desc = pos

    # Review finding #2(a)+(b): use the sibling's `id` (gid) from the
    # listing directly. The id is workspace-global, so we don't need to
    # filter by repository for top / bottom — and we'd be wrong to do
    # so, since "the first sub-issue of this parent" is a well-defined
    # concept across repos. For after / before the user supplied an
    # issue NUMBER (ambiguous across repos), so we still filter by the
    # cwd's repo to disambiguate.
    def _find_sibling_id_by_number_in_repo(num: int) -> str | None:
        for s in siblings:
            if s.get("number") != num:
                continue
            rep = s.get("repository") or {}
            if repos_match(
                {"ownerName": rep.get("owner"), "name": rep.get("name")},
                ctx.owner_repo,
            ):
                return s.get("id")
        return None

    if pos == "top":
        # First sibling whose id isn't ours (ids are workspace-global,
        # so we don't need a repo filter here).
        other = next(
            (s for s in siblings if s.get("id") and s.get("id") != child_id),
            None,
        )
        if other is None:
            return {
                "ok": False,
                "child_number": child_number,
                "parent_number": parent_number,
                "position": "top",
                "outcome": "noop",
                "error": (
                    f"#{child_number} is the only sub-issue of "
                    f"#{parent_number} — nothing to reorder against"
                ),
            }
        before_id = other.get("id")
        position_desc = "top"
    elif pos == "bottom":
        last_other = None
        for s in reversed(siblings):
            if s.get("id") and s.get("id") != child_id:
                last_other = s
                break
        if last_other is None:
            return {
                "ok": False,
                "child_number": child_number,
                "parent_number": parent_number,
                "position": "bottom",
                "outcome": "noop",
                "error": (
                    f"#{child_number} is the only sub-issue of "
                    f"#{parent_number} — nothing to reorder against"
                ),
            }
        after_id = last_other.get("id")
        position_desc = "bottom"
    elif pos == "after":
        sib_id = _find_sibling_id_by_number_in_repo(sibling_number)
        if not sib_id:
            return {
                "ok": False,
                "child_number": child_number,
                "parent_number": parent_number,
                "position": f"after #{sibling_number}",
                "outcome": "fail",
                "error": (
                    f"Anchor #{sibling_number} not found among sub-issues of "
                    f"#{parent_number} in {ctx.owner_repo}"
                ),
            }
        after_id = sib_id
        position_desc = f"after #{sibling_number}"
    elif pos == "before":
        sib_id = _find_sibling_id_by_number_in_repo(sibling_number)
        if not sib_id:
            return {
                "ok": False,
                "child_number": child_number,
                "parent_number": parent_number,
                "position": f"before #{sibling_number}",
                "outcome": "fail",
                "error": (
                    f"Anchor #{sibling_number} not found among sub-issues of "
                    f"#{parent_number} in {ctx.owner_repo}"
                ),
            }
        before_id = sib_id
        position_desc = f"before #{sibling_number}"

    # Belt-and-suspenders: if we somehow got here with neither anchor
    # set (a bug in the cases above), refuse to fire the mutation —
    # reprioritizeSubIssue with both ids null would return success and
    # leave the position unchanged, which is the silent-noop bug
    # review finding #2 called out.
    if after_id is None and before_id is None:
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": parent_number,
            "position": position_desc,
            "outcome": "fail",
            "error": (
                "Internal: no anchor resolved (both afterId and beforeId "
                "are null). Refusing to fire a vacuous reorder."
            ),
        }

    resp = ctx.query(
        _REPRIORITIZE_SUB_ISSUE_MUTATION,
        {
            "input": {
                "parentId": parent_id,
                "childIssueId": child_id,
                "afterId": after_id,
                "beforeId": before_id,
            }
        },
    )
    check_graphql_errors(resp, context="reprioritizeSubIssue")
    payload = (resp.get("data") or {}).get("reprioritizeSubIssue") or {}
    success = bool(payload.get("success"))
    github_errors = payload.get("githubErrors") or None
    if isinstance(github_errors, dict) and not github_errors:
        github_errors = None

    if not success:
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": parent_number,
            "position": position_desc,
            "outcome": "fail",
            "error": f"API rejected reorder; githubErrors={github_errors}",
        }
    return {
        "ok": True,
        "child_number": child_number,
        "parent_number": parent_number,
        "position": position_desc,
        "outcome": "ok",
        "error": None,
    }


# =============================================================================
# Sprints
# =============================================================================

_SPRINTS_QUERY_OPEN = """
query($workspaceId: ID!, $after: String) {
  workspace(id: $workspaceId) {
    id
    name
    activeSprint { id name }
    sprints(first: 50, after: $after, filters: { state: { eq: OPEN } }) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        name
        state
        startAt
        endAt
        completedPoints
        totalPoints
        closedIssuesCount
      }
    }
  }
}
"""

_SPRINTS_QUERY_ALL = """
query($workspaceId: ID!, $after: String) {
  workspace(id: $workspaceId) {
    id
    name
    activeSprint { id name }
    sprints(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        name
        state
        startAt
        endAt
        completedPoints
        totalPoints
        closedIssuesCount
      }
    }
  }
}
"""


def _serialize_sprint(node: dict, *, active_id: str | None) -> dict:
    # Use 0.0 (not bare 0) as the points-not-set fallback so callers
    # don't see int when the field is documented as float. Review #10.
    return {
        "id": node.get("id"),
        "name": node.get("name") or "",
        "state": node.get("state") or "",
        "start_at": node.get("startAt") or None,
        "end_at": node.get("endAt") or None,
        "completed_points": node.get("completedPoints") or 0.0,
        "total_points": node.get("totalPoints") or 0.0,
        "closed_issues_count": node.get("closedIssuesCount") or 0,
        "is_active": bool(active_id) and node.get("id") == active_id,
    }


def list_sprints(ctx: RepoContext, *, include_closed: bool = False) -> dict:
    """List sprints in the workspace.

    Walks `sprints` pagination — workspaces with >50 sprints would
    otherwise have older entries invisible to name resolution. Review
    finding #5.

    Args:
        include_closed: when True, returns OPEN + CLOSED sprints. Defaults
            to OPEN only.

    Returns:
        dict with:
            ok: bool
            workspace_name: str
            active_sprint_id: str | None
            sprints: list[dict] — each {id, name, state, start_at, end_at,
                completed_points, total_points, closed_issues_count,
                is_active}
            pagination_warning: str | None
    """
    query = _SPRINTS_QUERY_ALL if include_closed else _SPRINTS_QUERY_OPEN
    workspace_name = ""
    active_id: str | None = None
    nodes: list[dict] = []
    cursor: str | None = None
    last_cursor: str | None = None
    iterations = 0
    pagination_warning: str | None = None
    first_page = True

    while True:
        iterations += 1
        if iterations > MAX_PAGINATION_ITERATIONS:
            pagination_warning = (
                f"Sprint pagination iteration cap "
                f"({MAX_PAGINATION_ITERATIONS}) exceeded — bailing"
            )
            break
        resp = ctx.query(
            query,
            {"workspaceId": ctx.workspace_id, "after": cursor},
        )
        check_graphql_errors(resp, context="list_sprints")
        data = resp.get("data") or {}
        if "workspace" not in data or data.get("workspace") is None:
            # GraphQL returns `data.workspace = null` when the
            # workspace is deleted, ACL-revoked, or otherwise
            # unresolvable. An empty-but-real workspace returns a
            # non-null Workspace with `sprints.nodes = []`, which is
            # distinct. Pre-fix this branch collapsed null → {} and
            # silently returned ok=True with an empty sprint list,
            # indistinguishable from a real empty workspace.
            raise ZhApiError(
                f"Workspace {ctx.workspace_id!r} resolved to null "
                "(deleted, ACL-revoked, or otherwise inaccessible)"
            )
        ws = data["workspace"]
        if first_page:
            workspace_name = ws.get("name") or ""
            active = ws.get("activeSprint") or {}
            active_id = active.get("id")
            first_page = False
        conn = ws.get("sprints") or {}
        for n in conn.get("nodes") or []:
            # Skip null page entries defensively (matrix-3): API
            # contract is non-null but unexpected shapes shouldn't
            # crash the listing.
            if n is None:
                continue
            nodes.append(n)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        end_cursor = page_info.get("endCursor")
        if not end_cursor or end_cursor == last_cursor:
            pagination_warning = (
                "Sprint pagination cursor not advancing across requests "
                "— server likely mis-reporting hasNextPage. Bailing."
            )
            break
        last_cursor = end_cursor
        cursor = end_cursor

    return {
        "ok": True,
        "workspace_name": workspace_name,
        "active_sprint_id": active_id,
        "sprints": [_serialize_sprint(n, active_id=active_id) for n in nodes],
        "pagination_warning": pagination_warning,
    }


# Sprint detail comes in two pieces — the sprint header (single page,
# always one node) and its issues (paginated). Keeping them as separate
# queries lets us walk only the issue connection without re-fetching
# the header for every page.

_SPRINT_HEADER_QUERY = """
query($sprintId: ID!) {
  node(id: $sprintId) {
    ... on Sprint {
      id
      name
      description
      state
      startAt
      endAt
      completedPoints
      totalPoints
      closedIssuesCount
    }
  }
}
"""

_SPRINT_ISSUES_PAGE_QUERY = """
query($sprintId: ID!, $after: String) {
  node(id: $sprintId) {
    ... on Sprint {
      sprintIssues(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          issue {
            number
            title
            state
            htmlUrl
            estimate { value }
            assignees { nodes { login } }
            repository { ownerName name }
            pipelineIssues {
              nodes { pipeline { name } }
            }
          }
        }
      }
    }
  }
}
"""


def _find_sprint_id(
    ctx: RepoContext, sprint_name: str
) -> tuple[str | None, str | None, str | None]:
    """Resolve a sprint_id by name (case-insensitive) or "current"/"active".

    Returns (sprint_id, sprint_name, error). Exactly one of sprint_id /
    error is non-None.

    Walks paginated sprints (see list_sprints) so workspaces with >50
    sprints can still resolve older names.
    """
    want = (sprint_name or "").strip()
    if not want:
        # Empty-string lookups would otherwise match no sprint and dump
        # the full list as "available". Refuse early. (Review note.)
        return (
            None,
            None,
            "Sprint name must be non-empty (or use 'current' / 'active')",
        )
    listing = list_sprints(ctx, include_closed=True)
    sprints = listing.get("sprints") or []
    active_id = listing.get("active_sprint_id")
    listing_pagination_warning = listing.get("pagination_warning")
    if want.lower() in {"current", "active"}:
        if not active_id:
            return None, None, "No active sprint in this workspace"
        for s in sprints:
            if s.get("id") == active_id:
                return active_id, s.get("name"), None
        # Active sprint may not appear in our walked nodes (server
        # quirk); fall back to active id with empty name.
        return active_id, "", None
    want_lc = want.lower()
    for s in sprints:
        if (s.get("name") or "").lower() == want_lc:
            return s.get("id"), s.get("name"), None
    available = ", ".join(s.get("name") or "?" for s in sprints) or "(none)"
    # Round-6 #11: when the sprints listing bailed mid-walk, a
    # "not found" message is misleading — the sprint may exist on
    # an unreached page. Tell the user the listing was incomplete
    # so they can re-run rather than assume the sprint doesn't
    # exist.
    if listing_pagination_warning:
        return (
            None,
            None,
            (
                f"Sprint {sprint_name!r} not found in the first "
                f"{len(sprints)} walked sprints — listing was "
                f"incomplete due to: {listing_pagination_warning}. "
                f"Try re-running, or provide an exact sprint id."
            ),
        )
    return (
        None,
        None,
        f"Sprint {sprint_name!r} not found. Available: {available}",
    )


def _walk_sprint_issues(
    ctx: RepoContext, sprint_id: str
) -> tuple[list[dict], set[int], str | None]:
    """Walk every page of a sprint's `sprintIssues` connection.

    Returns (issue_dicts, walked_numbers, pagination_warning).

    * `issue_dicts` — repo-filtered, fully-decoded issue records used
      by `get_sprint_detail`'s `issues` list.
    * `walked_numbers` — every issue number the walker iterated, PRE-
      REPO-FILTER. Load-bearing for the round-5 #1 succeeded-
      inflation fix: `remove_issues_from_sprint` needs to know
      which inputs the walker actually saw, separately from which
      ones are in our repo, so partial-coverage classification can
      distinguish "walker observed it absent" from "walker never
      reached it."
    * `pagination_warning` — set when the walk bailed defensively
      (stuck cursor or iteration cap).

    Raises ZhApiError when the GraphQL response has `data.node = null`
    (deleted sprint, ACL revoked between resolution and walk, race
    condition during membership-edit pipelines). The empty-list-with-
    no-warning shape that the function used to return on null-node was
    indistinguishable from a real empty sprint, which let downstream
    callers (notably the explicit-reread recovery path in
    `remove_issues_from_sprint`) silently claim every input was
    removed.
    """
    out: list[dict] = []
    walked_numbers: set[int] = set()
    cursor: str | None = None
    last_cursor: str | None = None
    iterations = 0
    pagination_warning: str | None = None

    while True:
        iterations += 1
        if iterations > MAX_PAGINATION_ITERATIONS:
            pagination_warning = (
                f"Sprint-issues pagination iteration cap "
                f"({MAX_PAGINATION_ITERATIONS}) exceeded — bailing"
            )
            break
        resp = ctx.query(
            _SPRINT_ISSUES_PAGE_QUERY,
            {"sprintId": sprint_id, "after": cursor},
        )
        check_graphql_errors(resp, context="sprint_issues_page")
        data = resp.get("data") or {}
        if "node" not in data or data.get("node") is None:
            # data.node is null per GraphQL contract when the targeted
            # Sprint can't be resolved (deleted between query phases,
            # ACL change, etc.). Distinct from a real sprint with
            # zero issues — never silently swallow.
            raise ZhApiError(
                f"Sprint {sprint_id!r} resolved to null in "
                f"sprintIssues walk (deleted, ACL-revoked, or "
                f"otherwise inaccessible)"
            )
        node = data["node"]
        conn = node.get("sprintIssues") or {}
        for wrapper in conn.get("nodes") or []:
            # Defensive: skip null page entries the way the other two
            # walkers (list_sub_issues, list_sprints) do, rather than
            # appending a phantom `{number: None, title: ""}` record.
            # `remove_issues_from_sprint` filters phantoms via
            # `isinstance(num, int)` so it's unaffected, but
            # `get_sprint_detail` leaks the phantom into its `issues`
            # list. Matrix gap from round 4 finding #4.
            if wrapper is None:
                continue
            issue = (wrapper or {}).get("issue")
            # Round-6 #6: defend against `wrapper = {"issue": null}` —
            # `(wrapper or {}).get("issue") or {}` (the prior idiom)
            # coalesced None to `{}` and leaked a phantom
            # `{number: None, title: ""}` record into get_sprint_detail.
            # Skip explicitly here, same as the wrapper-None guard above.
            if issue is None:
                continue
            # Record iterated issue numbers IN OUR REPO so downstream
            # callers can distinguish "walker reached this input and
            # confirmed its absence" from "walker never reached this
            # input." Round-5 #1 tracked numbers pre-repo-filter, but
            # `still_attached_numbers` is post-repo-filter — a
            # cross-repo same-number trivially satisfied "walked AND
            # not still-attached" and got reported as `succeeded`
            # under partial coverage. Round-6 #1 fixes that by
            # repo-filtering walked_numbers too.
            walked_num = issue.get("number")
            if (
                isinstance(walked_num, int)
                and not isinstance(walked_num, bool)
                and repos_match(issue.get("repository"), ctx.owner_repo)
            ):
                walked_numbers.add(walked_num)
            assignees = [
                a.get("login")
                for a in ((issue.get("assignees") or {}).get("nodes") or [])
                if a.get("login")
            ]
            pipeline_nodes = (
                ((issue.get("pipelineIssues") or {}).get("nodes")) or []
            )
            pipeline_name = None
            # Defensive: pipeline_nodes[0] may itself be None
            # (unexpected shape; the schema says non-null but matrix
            # tests pin the surprise). Use `or {}` to coalesce.
            if pipeline_nodes:
                first_pn = pipeline_nodes[0] or {}
                pl = first_pn.get("pipeline") or {}
                pipeline_name = pl.get("name") or None
            rep = issue.get("repository") or {}
            est = issue.get("estimate") or {}
            out.append({
                "number": issue.get("number"),
                "title": issue.get("title") or "",
                "state": issue.get("state") or "UNKNOWN",
                "html_url": issue.get("htmlUrl") or "",
                "estimate": est.get("value"),
                "assignees": assignees,
                "pipeline": pipeline_name,
                "repository": {
                    "owner": rep.get("ownerName") or "",
                    "name": rep.get("name") or "",
                },
            })
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        end_cursor = page_info.get("endCursor")
        if not end_cursor or end_cursor == last_cursor:
            pagination_warning = (
                "Sprint-issues pagination cursor not advancing — server "
                "likely mis-reporting hasNextPage. Bailing."
            )
            break
        last_cursor = end_cursor
        cursor = end_cursor

    return out, walked_numbers, pagination_warning


def get_sprint_detail(ctx: RepoContext, sprint_name: str) -> dict:
    """Get detail + issues for a sprint named `sprint_name`.

    `sprint_name` accepts the special strings "current" or "active" to
    target the workspace's active sprint.

    Pagination: walks every page of `sprintIssues` so sprints with >100
    issues are reported fully (review finding #4).

    Raises:
        ZhApiError — propagated from the underlying walkers when:
          * `data.workspace = null` during sprint-name resolution
            (deleted / ACL-revoked workspace); `_find_sprint_id` calls
            `list_sprints` which raises in that case.
          * `data.node = null` during the issues-page walk (sprint
            deleted between phases / ACL change mid-walk);
            `_walk_sprint_issues` raises in that case.

        Round-5 #4: this is the canonical SPEC for direct callers
        (tests, internal helpers). The MCP wrapper `sprint_show` in
        mcp_server.py catches ZhApiError and converts to a structured
        result dict so MCP callers see a uniform shape. The same SPEC
        applies to `add_issues_to_sprint` / `remove_issues_from_sprint`
        via `_find_sprint_id → list_sprints`.

    Returns:
        dict with:
            ok: bool
            sprint_id: str | None
            sprint_name: str
            state: str | None
            start_at: str | None
            end_at: str | None
            completed_points: float
            total_points: float
            closed_issues_count: int
            description: str | None
            issue_count: int
            issues: list[dict] — each {number, title, state, html_url,
                estimate, assignees, pipeline, repository}
            pagination_warning: str | None
            error: str | None
    """
    sprint_id, actual_name, err = _find_sprint_id(ctx, sprint_name)
    if err or not sprint_id:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "state": None,
            "start_at": None,
            "end_at": None,
            "completed_points": 0.0,
            "total_points": 0.0,
            "closed_issues_count": 0,
            "description": None,
            "issue_count": 0,
            "issues": [],
            "pagination_warning": None,
            "error": err,
        }

    # Two queries: header (single page) + paginated sprintIssues walk.
    header_resp = ctx.query(_SPRINT_HEADER_QUERY, {"sprintId": sprint_id})
    check_graphql_errors(header_resp, context="sprint_header")
    node = (header_resp.get("data") or {}).get("node") or {}

    # `walked_numbers` ignored here — `get_sprint_detail` doesn't
    # need partial-coverage classification; it just emits whatever
    # the walker observed.
    issues, _walked_numbers, pagination_warning = _walk_sprint_issues(
        ctx, sprint_id
    )

    return {
        "ok": True,
        "sprint_id": sprint_id,
        "sprint_name": node.get("name") or actual_name or sprint_name,
        "state": node.get("state") or None,
        "start_at": node.get("startAt") or None,
        "end_at": node.get("endAt") or None,
        # 0.0 fallback preserves float type (review #10).
        "completed_points": node.get("completedPoints") or 0.0,
        "total_points": node.get("totalPoints") or 0.0,
        "closed_issues_count": node.get("closedIssuesCount") or 0,
        "description": node.get("description") or None,
        "issue_count": len(issues),
        "issues": issues,
        "pagination_warning": pagination_warning,
        "error": None,
    }


def get_current_sprint(ctx: RepoContext) -> dict:
    """Convenience wrapper: detail of the workspace's active sprint."""
    return get_sprint_detail(ctx, "current")


# =============================================================================
# Sprint membership mutations: addIssuesToSprints / removeIssuesFromSprints
#
# Both mutations accept arrays of issue and sprint ids. Unlike
# addSubIssues, the payloads do NOT include a `failedIssues` array —
# they return the resulting SprintIssue links (add) or the resulting
# Sprint state (remove). To detect partial failures we compare the
# requested input against what the API tells us came back:
#
#   - add: a SprintIssue with `issue.number == N` and `sprint.id == S`
#     in the response means N was added to S. Inputs missing from the
#     response are inferred-failed (the GraphQL endpoint accepts the
#     request without telling us WHY a specific issue didn't link).
#
#   - remove: the response is the post-mutation sprint state. Anything
#     STILL in `sprint.sprintIssues` that we asked to remove is
#     inferred-failed.
# =============================================================================

_ADD_ISSUES_TO_SPRINTS_MUTATION = """
mutation($input: AddIssuesToSprintsInput!) {
  addIssuesToSprints(input: $input) {
    sprintIssues {
      id
      issue {
        number
        repository { ownerName name }
      }
      sprint { id }
    }
  }
}
"""

_REMOVE_ISSUES_FROM_SPRINTS_MUTATION = """
mutation($input: RemoveIssuesFromSprintsInput!) {
  removeIssuesFromSprints(input: $input) {
    sprints {
      id
      sprintIssues(first: 100) {
        nodes {
          issue {
            number
            repository { ownerName name }
          }
        }
      }
    }
  }
}
"""


def _resolve_issue_ids_in_repo(
    ctx: RepoContext, issue_numbers: list[int]
) -> tuple[dict[int, str], list[int]]:
    """Look up GraphQL ids for issue numbers in the ctx's repo.

    Returns `({number: gid}, not_found_numbers)`. Pre-flight validation
    so we can fail cleanly on typos before firing a mutation.

    Note on duplicates: this function returns a `{number: gid}` dict, so
    duplicate input numbers collapse to a single entry. Dedup should
    happen at the CALL SITE (with `dict.fromkeys` to preserve order)
    BEFORE handing the list here, otherwise the caller's `issue_numbers`
    keeps the duplicates and the success/failure accounting can over-
    report. The two mutation entrypoints below do this explicitly.
    """
    out: dict[int, str] = {}
    missing: list[int] = []
    for n in issue_numbers:
        info = get_issue_by_info(ctx, n)
        if not info or not info.get("id"):
            missing.append(n)
        else:
            out[n] = info["id"]
    return out, missing


def add_issues_to_sprint(
    ctx: RepoContext, sprint_name: str, issue_numbers: list[int]
) -> dict:
    """Add one or more issues to a sprint.

    Identifies partial failures by comparing the API's returned
    `sprintIssues` against the input set. Issues that don't appear in
    the response are inferred-failed — the API doesn't tell us WHY
    (e.g. issue already in the sprint, archived, etc.), only that the
    new link didn't materialize. The link's `issue.repository` is
    cross-checked against `ctx.owner_repo` (case-insensitive) so a
    sibling repo's same-numbered issue can't accidentally count as
    success in a multi-repo workspace.

    Duplicate input numbers are collapsed first-occurrence (e.g.
    `[42, 42, 43]` is treated as `[42, 43]`) so the success accounting
    can't be inflated by repeating an input.

    Args:
        ctx: RepoContext.
        sprint_name: target sprint by name. `current` / `active` are
            aliases for the workspace's active sprint.
        issue_numbers: list of positive ints (issue numbers in the
            ctx's repo).

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            sprint_id: str | None
            sprint_name: str
            outcome: "ok" | "partial" | "fail" | "noop"
            success_count: int
            failed_count: int
            succeeded: list[int] — input numbers the API actually linked
            failed: list[int] — input numbers absent from the API's response
            error: str | None
    """
    if not issue_numbers:
        raise ZhApiError("issue_numbers must be non-empty")
    for n in issue_numbers:
        if not _is_positive_int(n):
            raise ZhApiError(
                f"every issue number must be a positive int (got {n!r})"
            )

    # Dedup at the boundary — preserves first-occurrence order so the
    # output `succeeded` / `failed` lists are stable across calls.
    # Review finding #10.
    issue_numbers = list(dict.fromkeys(issue_numbers))

    sprint_id, actual_sprint_name, err = _find_sprint_id(ctx, sprint_name)
    if err or not sprint_id:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "error": err,
        }

    issue_ids, missing = _resolve_issue_ids_in_repo(ctx, issue_numbers)
    if missing:
        return {
            "ok": False,
            "sprint_id": sprint_id,
            "sprint_name": actual_sprint_name or sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": len(missing),
            "succeeded": [],
            "failed": missing,
            "error": (
                "Some issue numbers were not found in this repository: "
                + ", ".join(f"#{n}" for n in missing)
            ),
        }

    resp = ctx.query(
        _ADD_ISSUES_TO_SPRINTS_MUTATION,
        {
            "input": {
                "issueIds": list(issue_ids.values()),
                "sprintIds": [sprint_id],
            }
        },
    )
    check_graphql_errors(resp, context="addIssuesToSprints")
    payload = (resp.get("data") or {}).get("addIssuesToSprints") or {}
    returned_links = payload.get("sprintIssues") or []

    # The API returns one SprintIssue per (issue, sprint) link that was
    # created. Filter to links for THIS sprint AND in the ctx's repo
    # before pulling issue numbers — without the repo filter, a
    # sibling repo's same-numbered issue could falsely register as
    # success in a multi-repo workspace. Review finding #8.
    succeeded_numbers: set[int] = set()
    for link in returned_links:
        sprint = (link or {}).get("sprint") or {}
        if sprint.get("id") != sprint_id:
            continue
        issue = (link or {}).get("issue") or {}
        if not repos_match(issue.get("repository"), ctx.owner_repo):
            continue
        num = issue.get("number")
        if isinstance(num, int):
            succeeded_numbers.add(num)

    succeeded = [n for n in issue_numbers if n in succeeded_numbers]
    failed = [n for n in issue_numbers if n not in succeeded_numbers]
    outcome = _classify_outcome(len(succeeded), len(failed))

    return {
        "ok": outcome == "ok",
        "sprint_id": sprint_id,
        "sprint_name": actual_sprint_name or sprint_name,
        "outcome": outcome,
        "success_count": len(succeeded),
        "failed_count": len(failed),
        "succeeded": succeeded,
        "failed": failed,
        "error": None,
    }


def remove_issues_from_sprint(
    ctx: RepoContext, sprint_name: str, issue_numbers: list[int]
) -> dict:
    """Remove one or more issues from a sprint.

    Identifies partial failures by checking which input numbers are
    STILL attached to the sprint AFTER the mutation. The mutation
    response includes the sprint's first 100 sprintIssues; we walk all
    pages when the response was full (>100 issues) and ALSO when the
    response didn't include the target sprint at all (a documented
    API contract anomaly we recover from rather than fail loudly).

    Each post-state issue's `repository` is filtered against
    `ctx.owner_repo` (case-insensitive) before its number lands in the
    "still attached" set — without this, a sibling repo's same-
    numbered issue in the sprint could mis-classify our removal as a
    failure.

    Duplicate input numbers are collapsed first-occurrence before any
    counting happens.

    Returns:
        dict with the same shape as add_issues_to_sprint, plus:
            inspected_full: bool — True if we walked every page (or
                if the mutation response had <100 nodes so we knew
                the response was complete).
            pagination_warning: str | None — propagated from the
                follow-up walk if the walker bailed defensively
                (stuck cursor or iteration cap).
            response_anomaly: str | None — set if the mutation
                response omitted the target sprint, returned an empty
                `sprints` array, OR the post-state walk could only
                reach part of the sprint (see below). Always paired
                with enough text to act on.

    Coverage semantics
    ------------------

    When `inspected_full` is True (mutation response was complete OR
    we walked every page without bailing), the SPEC is simple:
    `succeeded = inputs - still_attached`, `failed = inputs ∩
    still_attached`, and `success_count + failed_count == len(inputs)`.

    When `inspected_full` is False (walker bailed mid-walk on stuck
    cursor / iteration cap), `still_attached_numbers` is by
    construction a strict subset of what's actually attached. Inputs
    the walker never reached can't be classified as succeeded OR
    failed — we never confirmed anything about them. The honest SPEC
    is:

      - `succeeded = inputs ∩ walked_numbers - still_attached` —
        inputs the walker actually observed AND observed as absent
        from the post-state.
      - `failed = inputs ∩ walked_numbers ∩ still_attached` —
        inputs the walker observed still-attached.
      - inputs in `walked_numbers` neither succeeded nor failed —
        they're un-verified, surfaced as a count in
        `response_anomaly` rather than in `succeeded` / `failed`.
      - `outcome = "partial"` when at least one input was confirmed
        removed by the partial walk, `"fail"` otherwise.
      - `ok = (outcome == "ok")` — so partial coverage always
        reports `ok=False`.

    `walked_numbers` itself is not part of the returned dict (it's
    an implementation detail of the partial-coverage classification),
    but the count of un-verified inputs is named in `response_anomaly`
    so callers can see it directly. Re-verification command is
    `zh sprint show '<name>'`.
    """
    if not issue_numbers:
        raise ZhApiError("issue_numbers must be non-empty")
    for n in issue_numbers:
        if not _is_positive_int(n):
            raise ZhApiError(
                f"every issue number must be a positive int (got {n!r})"
            )

    # Same dedup boundary as add. Review finding #10.
    issue_numbers = list(dict.fromkeys(issue_numbers))

    sprint_id, actual_sprint_name, err = _find_sprint_id(ctx, sprint_name)
    if err or not sprint_id:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "error": err,
        }

    issue_ids, missing = _resolve_issue_ids_in_repo(ctx, issue_numbers)
    if missing:
        return {
            "ok": False,
            "sprint_id": sprint_id,
            "sprint_name": actual_sprint_name or sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": len(missing),
            "succeeded": [],
            "failed": missing,
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "error": (
                "Some issue numbers were not found in this repository: "
                + ", ".join(f"#{n}" for n in missing)
            ),
        }

    resp = ctx.query(
        _REMOVE_ISSUES_FROM_SPRINTS_MUTATION,
        {
            "input": {
                "issueIds": list(issue_ids.values()),
                "sprintIds": [sprint_id],
            }
        },
    )
    check_graphql_errors(resp, context="removeIssuesFromSprints")
    payload = (resp.get("data") or {}).get("removeIssuesFromSprints") or {}
    sprints_after = payload.get("sprints") or []

    # Locate the target sprint's post-state in the response.
    target_sprint = next(
        (s for s in sprints_after if (s or {}).get("id") == sprint_id),
        None,
    )

    # Build the still-attached set + the walked set, filtering
    # still-attached by repo so a sibling repo's same-numbered issue
    # can't mis-classify our removal as a failure (review #3). The
    # walked set is PRE-repo-filter so we can answer "did the walker
    # actually observe this input number?" — load-bearing for the
    # partial-coverage classification below.
    def _attached_from_nodes(
        nodes: list[dict],
    ) -> tuple[set[int], set[int]]:
        """Returns (still_attached_in_repo, walked_numbers_any_repo)."""
        attached: set[int] = set()
        walked: set[int] = set()
        for n_link in nodes or []:
            issue = (n_link or {}).get("issue") or {}
            num = issue.get("number")
            if isinstance(num, int) and not isinstance(num, bool):
                walked.add(num)
            if not repos_match(issue.get("repository"), ctx.owner_repo):
                continue
            if isinstance(num, int) and not isinstance(num, bool):
                attached.add(num)
        return attached, walked

    still_attached_numbers: set[int] = set()
    walked_numbers: set[int] = set()
    inspected_full = False
    pagination_warning: str | None = None
    response_anomaly: str | None = None

    if target_sprint is None:
        # Review finding #1: the response's `sprints: [Sprint!]!` field
        # is non-null-list-of-non-null-sprint per schema, so an empty
        # array or a response missing our sprint id is anomalous. The
        # mutation may well have succeeded; we just lost the post-
        # state reference. Recover by walking the sprint directly
        # rather than silently treating every input as removed.
        if not sprints_after:
            response_anomaly = (
                "Mutation response had an empty `sprints` array; "
                "walked sprint directly to determine post-state."
            )
        else:
            returned_ids = [
                (s or {}).get("id") for s in sprints_after if s
            ]
            response_anomaly = (
                f"Mutation response did not include sprint {sprint_id!r} "
                f"in its `sprints` array (got: {returned_ids!r}); "
                "walked sprint directly to determine post-state."
            )
        # The walker raises ZhApiError on null-node; let that propagate
        # as a structured failure result rather than crashing the whole
        # MCP tool. The mutation may have already taken effect; we just
        # can't determine the post-state, which is fail-state semantics.
        try:
            walked, walked_numbers, walk_warning = _walk_sprint_issues(
                ctx, sprint_id,
            )
        except ZhApiError as walk_err:
            return {
                "ok": False,
                "sprint_id": sprint_id,
                "sprint_name": actual_sprint_name or sprint_name,
                "outcome": "fail",
                "success_count": 0,
                "failed_count": len(issue_numbers),
                "succeeded": [],
                "failed": list(issue_numbers),
                "inspected_full": False,
                "pagination_warning": None,
                "response_anomaly": (response_anomaly or "") + (
                    f" Recovery walk also failed: {walk_err}"
                ),
                "error": (
                    "Sprint post-state could not be determined: "
                    f"{walk_err}"
                ),
            }
        # The repository field from the walker is in
        # {"owner": ..., "name": ...} form, not {"ownerName", "name"};
        # adapt before passing to repos_match.
        for w in walked:
            rep = w.get("repository") or {}
            if not repos_match(
                {"ownerName": rep.get("owner"), "name": rep.get("name")},
                ctx.owner_repo,
            ):
                continue
            num = w.get("number")
            if isinstance(num, int) and not isinstance(num, bool):
                still_attached_numbers.add(num)
        # inspected_full reflects whether the walk completed without a
        # defensive bail (stuck cursor / iteration cap). When the
        # walker emits a warning the result is partial; pinning it
        # True would mis-advertise our coverage.
        inspected_full = walk_warning is None
        pagination_warning = walk_warning
    else:
        nodes = ((target_sprint.get("sprintIssues") or {}).get("nodes")) or []
        still_attached_numbers, walked_numbers = _attached_from_nodes(nodes)
        # If the response was full, walk for the rest.
        if len(nodes) >= 100:
            try:
                walked, walked_numbers, walk_warning = _walk_sprint_issues(
                    ctx, sprint_id,
                )
            except ZhApiError as walk_err:
                return {
                    "ok": False,
                    "sprint_id": sprint_id,
                    "sprint_name": actual_sprint_name or sprint_name,
                    "outcome": "fail",
                    "success_count": 0,
                    "failed_count": len(issue_numbers),
                    "succeeded": [],
                    "failed": list(issue_numbers),
                    "inspected_full": False,
                    "pagination_warning": None,
                    "response_anomaly": (
                        "Mutation response was full; follow-up walk "
                        f"to confirm post-state failed: {walk_err}"
                    ),
                    "error": (
                        "Sprint post-state could not be confirmed: "
                        f"{walk_err}"
                    ),
                }
            still_attached_numbers = set()
            for w in walked:
                rep = w.get("repository") or {}
                if not repos_match(
                    {"ownerName": rep.get("owner"), "name": rep.get("name")},
                    ctx.owner_repo,
                ):
                    continue
                num = w.get("number")
                if isinstance(num, int) and not isinstance(num, bool):
                    still_attached_numbers.add(num)
            # Same SPEC contract as the recovery branch above: walker
            # warning means partial coverage, so inspected_full is
            # False.
            inspected_full = walk_warning is None
            pagination_warning = walk_warning
        else:
            # Response had <100 nodes, so we know it was the whole
            # post-state. No walk needed; coverage is full.
            inspected_full = True

    # Compute succeeded/failed using the round-5 #1 SPEC.
    #
    # When inspected_full is True, our coverage is the full sprint
    # post-state (either the mutation response had <100 nodes, or we
    # walked all pages without bailing). In that case any input not in
    # `still_attached_numbers` is confirmed-removed.
    #
    # When inspected_full is False, the partial walk only saw part of
    # the sprint. Inputs that the walker DIDN'T reach can't be
    # classified as `succeeded` — we never confirmed their removal.
    # The honest `succeeded` set is `inputs ∩ walked_numbers -
    # still_attached_numbers`: inputs the walker actually observed
    # AND observed as absent from the post-state.
    if inspected_full:
        succeeded = [n for n in issue_numbers if n not in still_attached_numbers]
        failed = [n for n in issue_numbers if n in still_attached_numbers]
        outcome = _classify_outcome(len(succeeded), len(failed))
    else:
        succeeded = [
            n for n in issue_numbers
            if n in walked_numbers and n not in still_attached_numbers
        ]
        # `failed` for partial coverage: only count inputs the walker
        # actually saw still-attached. Inputs the walker never
        # reached are NEITHER succeeded NOR failed — they're
        # un-verified (reflected in `response_anomaly`'s coverage
        # note below). This keeps success_count + failed_count <=
        # len(inputs), so the counts honestly report what we
        # observed.
        failed = [
            n for n in issue_numbers
            if n in walked_numbers and n in still_attached_numbers
        ]
        unverified_count = len(issue_numbers) - len(succeeded) - len(failed)
        coverage_note = (
            f"Post-state coverage incomplete (inspected_full=False, "
            f"walker bailed: {pagination_warning or 'unknown reason'}); "
            f"verified {len(succeeded)} of {len(issue_numbers)} input(s) "
            f"as removed; "
            f"{unverified_count} input(s) un-verified — the walker "
            f"never reached them, so we cannot say whether the "
            f"mutation took effect. "
            f"Re-verify with `zh sprint show '{actual_sprint_name or sprint_name}'`."
        )
        if response_anomaly:
            response_anomaly = f"{response_anomaly} {coverage_note}"
        else:
            response_anomaly = coverage_note
        # outcome: "partial" when we confirmed at least one removal,
        # "fail" otherwise.
        outcome = "partial" if succeeded else "fail"

    return {
        "ok": outcome == "ok",
        "sprint_id": sprint_id,
        "sprint_name": actual_sprint_name or sprint_name,
        "outcome": outcome,
        "success_count": len(succeeded),
        "failed_count": len(failed),
        "succeeded": succeeded,
        "failed": failed,
        "inspected_full": inspected_full,
        "pagination_warning": pagination_warning,
        "response_anomaly": response_anomaly,
        "error": None,
    }
