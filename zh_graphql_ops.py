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


def get_issue_by_info(
    ctx: RepoContext, issue_number: int
) -> dict | None:
    """Fetch a single issue's structural info via the context's query method.

    Returns None when the issue doesn't exist in this repository. Raises
    ZhApiError for bad input or top-level GraphQL errors.
    """
    if not isinstance(issue_number, int) or issue_number <= 0:
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
        number
        title
        state
        assignees {
          nodes { login }
        }
        pipelineIssue(workspaceId: $workspaceId) {
          pipeline { name }
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
                number: int
                title: str (untruncated)
                state: "OPEN" | "CLOSED"
                pipeline: str | None
                assignees: list[str]
                repository: {"owner": str, "name": str}
            pagination_warning: str | None — set if we bailed defensively
    """
    if not isinstance(parent_number, int) or parent_number <= 0:
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
            assignees = [
                a.get("login")
                for a in ((node.get("assignees") or {}).get("nodes") or [])
                if a.get("login")
            ]
            pipeline_name = None
            pi = node.get("pipelineIssue") or None
            if pi:
                pl = pi.get("pipeline") or {}
                pipeline_name = pl.get("name") or None
            repo = node.get("repository") or {}
            children.append({
                "number": node.get("number"),
                "title": node.get("title") or "",
                "state": node.get("state") or "UNKNOWN",
                "pipeline": pipeline_name,
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
        if end_cursor and end_cursor == last_cursor:
            pagination_warning = (
                "Pagination cursor unchanged across requests — server "
                "likely mis-reporting hasNextPage. Bailing."
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
    if not isinstance(child_number, int) or child_number <= 0:
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
    """
    if not child_numbers:
        raise ZhApiError("child_numbers must be non-empty")
    for n in child_numbers:
        if not isinstance(n, int) or n <= 0:
            raise ZhApiError(
                f"every child number must be a positive int (got {n!r})"
            )

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
    succeeded = [n for n in child_numbers if n not in failed_numbers]
    # If the API didn't fail anything but also didn't succeed on anything,
    # we can't claim those were "succeeded" — keep both sides empty.
    if success_count == 0:
        succeeded = []

    outcome = _classify_outcome(success_count, failed_count)
    return {
        "ok": outcome == "ok",
        "parent_number": parent_number,
        "outcome": outcome,
        "success_count": success_count,
        "failed_count": failed_count,
        "succeeded": succeeded,
        "failed": failed_serialized,
        "github_errors": github_errors,
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
    """
    if not child_numbers:
        raise ZhApiError("child_numbers must be non-empty")
    for n in child_numbers:
        if not isinstance(n, int) or n <= 0:
            raise ZhApiError(
                f"every child number must be a positive int (got {n!r})"
            )

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
    succeeded = [n for n in child_numbers if n not in failed_numbers]
    if success_count == 0:
        succeeded = []

    outcome = _classify_outcome(success_count, failed_count)
    return {
        "ok": outcome == "ok",
        "parent_number": parent_number,
        "outcome": outcome,
        "success_count": success_count,
        "failed_count": failed_count,
        "succeeded": succeeded,
        "failed": failed_serialized,
        "github_errors": github_errors,
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
        if not isinstance(sibling_number, int) or sibling_number <= 0:
            raise ZhApiError(
                f"position {pos!r} requires a positive sibling_number "
                f"(got {sibling_number!r})"
            )
    else:
        raise ZhApiError(
            f"position must be one of top/bottom/after/before (got {position!r})"
        )

    # Resolve the child + its parent.
    if not isinstance(child_number, int) or child_number <= 0:
        raise ZhApiError(
            f"child_number must be a positive int (got {child_number!r})"
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

    after_id: str | None = None
    before_id: str | None = None
    position_desc = pos

    def _find_sibling_id(num: int) -> str | None:
        # Cross-repo finding #5: filter by (number, owner, repo)
        # case-insensitively to avoid colliding with same-number issues
        # in a different repo of this workspace.
        for s in siblings:
            if s.get("number") != num:
                continue
            rep = s.get("repository") or {}
            if repos_match(
                {"ownerName": rep.get("owner"), "name": rep.get("name")},
                ctx.owner_repo,
            ):
                # We need the GraphQL id of the sibling; we don't have it
                # from list_sub_issues. Fetch directly.
                got = get_issue_by_info(ctx, num)
                return (got or {}).get("id")
        return None

    if pos == "top":
        # First sibling other than self
        other = next(
            (
                s for s in siblings
                if s.get("number") != child_number
                or not repos_match(
                    {
                        "ownerName": (s.get("repository") or {}).get("owner"),
                        "name": (s.get("repository") or {}).get("name"),
                    },
                    ctx.owner_repo,
                )
            ),
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
        before_id = _find_sibling_id(other.get("number"))
        position_desc = "top"
    elif pos == "bottom":
        last_other = None
        for s in reversed(siblings):
            if s.get("number") == child_number and repos_match(
                {
                    "ownerName": (s.get("repository") or {}).get("owner"),
                    "name": (s.get("repository") or {}).get("name"),
                },
                ctx.owner_repo,
            ):
                continue
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
        after_id = _find_sibling_id(last_other.get("number"))
        position_desc = "bottom"
    elif pos == "after":
        sib_id = _find_sibling_id(sibling_number)
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
        sib_id = _find_sibling_id(sibling_number)
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
query($workspaceId: ID!) {
  workspace(id: $workspaceId) {
    id
    name
    activeSprint { id name }
    sprints(first: 50, filters: { state: { eq: OPEN } }) {
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
query($workspaceId: ID!) {
  workspace(id: $workspaceId) {
    id
    name
    activeSprint { id name }
    sprints(first: 50) {
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
    return {
        "id": node.get("id"),
        "name": node.get("name") or "",
        "state": node.get("state") or "",
        "start_at": node.get("startAt") or None,
        "end_at": node.get("endAt") or None,
        "completed_points": node.get("completedPoints") or 0,
        "total_points": node.get("totalPoints") or 0,
        "closed_issues_count": node.get("closedIssuesCount") or 0,
        "is_active": bool(active_id) and node.get("id") == active_id,
    }


def list_sprints(ctx: RepoContext, *, include_closed: bool = False) -> dict:
    """List sprints in the workspace.

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
    """
    query = _SPRINTS_QUERY_ALL if include_closed else _SPRINTS_QUERY_OPEN
    resp = ctx.query(query, {"workspaceId": ctx.workspace_id})
    check_graphql_errors(resp, context="list_sprints")
    ws = (resp.get("data") or {}).get("workspace") or {}
    active = ws.get("activeSprint") or {}
    active_id = active.get("id")
    nodes = ((ws.get("sprints") or {}).get("nodes")) or []
    return {
        "ok": True,
        "workspace_name": ws.get("name") or "",
        "active_sprint_id": active_id,
        "sprints": [_serialize_sprint(n, active_id=active_id) for n in nodes],
    }


_SPRINT_DETAIL_QUERY = """
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
      sprintIssues(first: 100) {
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
    """
    listing = list_sprints(ctx, include_closed=True)
    sprints = listing.get("sprints") or []
    active_id = listing.get("active_sprint_id")
    want = (sprint_name or "").strip()
    if want.lower() in {"current", "active"}:
        if not active_id:
            return None, None, "No active sprint in this workspace"
        for s in sprints:
            if s.get("id") == active_id:
                return active_id, s.get("name"), None
        # Active sprint may not appear in our 50 nodes (very unlikely);
        # fall back to active id with empty name.
        return active_id, "", None
    want_lc = want.lower()
    for s in sprints:
        if (s.get("name") or "").lower() == want_lc:
            return s.get("id"), s.get("name"), None
    available = ", ".join(s.get("name") or "?" for s in sprints) or "(none)"
    return (
        None,
        None,
        f"Sprint {sprint_name!r} not found. Available: {available}",
    )


def get_sprint_detail(ctx: RepoContext, sprint_name: str) -> dict:
    """Get detail + issues for a sprint named `sprint_name`.

    `sprint_name` accepts the special strings "current" or "active" to
    target the workspace's active sprint.

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
            "completed_points": 0,
            "total_points": 0,
            "closed_issues_count": 0,
            "description": None,
            "issue_count": 0,
            "issues": [],
            "error": err,
        }

    resp = ctx.query(_SPRINT_DETAIL_QUERY, {"sprintId": sprint_id})
    check_graphql_errors(resp, context="get_sprint_detail")
    node = (resp.get("data") or {}).get("node") or {}

    issues_raw = (
        ((node.get("sprintIssues") or {}).get("nodes")) or []
    )
    issues: list[dict] = []
    for wrapper in issues_raw:
        issue = (wrapper or {}).get("issue") or {}
        assignees = [
            a.get("login")
            for a in ((issue.get("assignees") or {}).get("nodes") or [])
            if a.get("login")
        ]
        pipeline_nodes = (
            ((issue.get("pipelineIssues") or {}).get("nodes")) or []
        )
        pipeline_name = None
        if pipeline_nodes:
            pl = pipeline_nodes[0].get("pipeline") or {}
            pipeline_name = pl.get("name") or None
        rep = issue.get("repository") or {}
        est = issue.get("estimate") or {}
        issues.append({
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

    return {
        "ok": True,
        "sprint_id": sprint_id,
        "sprint_name": node.get("name") or actual_name or sprint_name,
        "state": node.get("state") or None,
        "start_at": node.get("startAt") or None,
        "end_at": node.get("endAt") or None,
        "completed_points": node.get("completedPoints") or 0,
        "total_points": node.get("totalPoints") or 0,
        "closed_issues_count": node.get("closedIssuesCount") or 0,
        "description": node.get("description") or None,
        "issue_count": len(issues),
        "issues": issues,
        "error": None,
    }


def get_current_sprint(ctx: RepoContext) -> dict:
    """Convenience wrapper: detail of the workspace's active sprint."""
    return get_sprint_detail(ctx, "current")
