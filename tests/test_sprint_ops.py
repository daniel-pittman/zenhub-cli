"""Tests for sprint GraphQL operations.

Sprint functionality inspired by the design proposed in PR #2 by
@jeremiahrose; these tests exercise the Python rewrite against the
same workspace/Sprint GraphQL types that proposal used. Mutation
coverage (add / remove issues to sprints) lives at the bottom.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import zh_api
import zh_graphql_ops


def _ctx() -> zh_api.RepoContext:
    return zh_api.RepoContext(
        owner_repo="acme/widgets",
        repo_id="repo-gid-123",
        workspace_id="workspace-gid-456",
        token="fake-token",
    )


def _patch_ctx_query(ctx: zh_api.RepoContext, responses: list[dict]):
    """Replace ctx.query with a generator over `responses`.

    Each successive `ctx.query(...)` call consumes one entry. A test
    that under-supplies responses will get a StopIteration — the loud
    failure is intentional.
    """
    it = iter(responses)
    return patch.object(
        ctx, "query",
        side_effect=lambda query, variables=None: next(it),
    )


# =============================================================================
# Test fixture builders
# =============================================================================

def _sprints_page(nodes: list[dict], *, has_next: bool = False,
                  end_cursor: str | None = None,
                  workspace_name: str = "Backend Team",
                  active_id: str | None = "sprint-7") -> dict:
    """Build a single page of `workspace.sprints` response.

    The new query shape includes `pageInfo`; tests need to supply it
    even for single-page responses (with `hasNextPage=false`).
    """
    return {
        "data": {
            "workspace": {
                "id": "workspace-gid-456",
                "name": workspace_name,
                "activeSprint": (
                    {"id": active_id, "name": "Sprint 7"}
                    if active_id else None
                ),
                "sprints": {
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": end_cursor,
                    },
                    "nodes": nodes,
                },
            }
        }
    }


def _sprint_node(sprint_id: str, name: str, *, state: str = "OPEN",
                 start: str = "2026-05-01T00:00:00Z",
                 end: str = "2026-05-15T00:00:00Z",
                 completed: float = 5.0,
                 total: float = 13.0,
                 closed: int = 3) -> dict:
    return {
        "id": sprint_id,
        "name": name,
        "state": state,
        "startAt": start,
        "endAt": end,
        "completedPoints": completed,
        "totalPoints": total,
        "closedIssuesCount": closed,
    }


def _sprint_header_response(*, name: str = "Sprint 7",
                            description: str = "Stabilize the auth refactor",
                            state: str = "OPEN") -> dict:
    return {
        "data": {
            "node": {
                "id": "sprint-7",
                "name": name,
                "description": description,
                "state": state,
                "startAt": "2026-05-01T00:00:00Z",
                "endAt": "2026-05-15T00:00:00Z",
                "completedPoints": 5,
                "totalPoints": 13,
                "closedIssuesCount": 3,
            }
        }
    }


def _issue_node(number: int, *, title: str | None = None,
                state: str = "OPEN",
                estimate: int | None = 3,
                assignees: list[str] | None = None,
                pipeline: str | None = "In Progress",
                owner: str = "acme",
                repo_name: str = "widgets") -> dict:
    return {
        "issue": {
            "number": number,
            "title": title or f"Issue {number}",
            "state": state,
            "htmlUrl": f"https://github.com/{owner}/{repo_name}/issues/{number}",
            "estimate": ({"value": estimate} if estimate is not None else None),
            "assignees": {
                "nodes": [{"login": a} for a in (assignees or [])]
            },
            "repository": {"ownerName": owner, "name": repo_name},
            "pipelineIssues": {
                "nodes": (
                    [{"pipeline": {"name": pipeline}}] if pipeline else []
                )
            },
        }
    }


def _sprint_issues_page(nodes: list[dict], *, has_next: bool = False,
                        end_cursor: str | None = None) -> dict:
    return {
        "data": {
            "node": {
                "sprintIssues": {
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": end_cursor,
                    },
                    "nodes": nodes,
                }
            }
        }
    }


# =============================================================================
# list_sprints
# =============================================================================

def test_list_sprints_open_only_marks_active():
    """The active sprint's id is marked with is_active=True."""
    ctx = _ctx()
    response = _sprints_page([
        _sprint_node("sprint-7", "Sprint 7"),
        _sprint_node("sprint-8", "Sprint 8",
                     start="2026-05-15T00:00:00Z",
                     end="2026-05-29T00:00:00Z",
                     completed=0.0, total=0.0, closed=0),
    ])
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sprints(ctx)
    assert out["ok"] is True
    assert out["workspace_name"] == "Backend Team"
    assert out["active_sprint_id"] == "sprint-7"
    names_active = {(s["name"], s["is_active"]) for s in out["sprints"]}
    assert names_active == {("Sprint 7", True), ("Sprint 8", False)}


def test_list_sprints_handles_empty_workspace():
    ctx = _ctx()
    response = _sprints_page([], active_id=None, workspace_name="Empty Team")
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sprints(ctx)
    assert out["ok"] is True
    assert out["sprints"] == []
    assert out["active_sprint_id"] is None


def test_list_sprints_walks_pagination_for_name_lookup():
    """Workspaces with >50 sprints — name resolution must walk pages.

    Review finding #5: a name lookup in `_find_sprint_id` against a
    workspace whose target sprint is on page 2 would otherwise silently
    return "not found". This test verifies both list_sprints (which
    backs _find_sprint_id) walks every page.
    """
    ctx = _ctx()
    page_one_nodes = [_sprint_node(f"sprint-{i}", f"Sprint {i}",
                                   state="OPEN") for i in range(1, 51)]
    page_two_nodes = [_sprint_node("sprint-old", "Sprint Old", state="OPEN")]
    responses = [
        _sprints_page(page_one_nodes, has_next=True, end_cursor="cursor-2"),
        _sprints_page(page_two_nodes, has_next=False),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.list_sprints(ctx)
    names = {s["name"] for s in out["sprints"]}
    assert "Sprint Old" in names
    assert len(out["sprints"]) == 51
    assert out["pagination_warning"] is None


def test_list_sprints_stuck_cursor_bails():
    """If endCursor stops advancing (or is None) while hasNextPage=true."""
    ctx = _ctx()
    stuck = _sprints_page(
        [_sprint_node("sprint-1", "Sprint 1")],
        has_next=True,
        end_cursor=None,  # missing cursor is the bug we guard against
    )
    with _patch_ctx_query(ctx, [stuck, stuck]):
        out = zh_graphql_ops.list_sprints(ctx)
    assert "cursor not advancing" in (out["pagination_warning"] or "").lower()


def test_list_sprints_serializes_points_as_float():
    """Review #10: completed_points / total_points kept as floats.

    Bug was `node.get("completedPoints") or 0` — when the API returned
    literal 0.0, the `or 0` arm coerced to int. With the fixed `or 0.0`
    fallback, missing values are floats too.
    """
    ctx = _ctx()
    response = _sprints_page([
        # Sprint with explicit non-zero floats — should round-trip
        _sprint_node("sprint-A", "A", completed=5.5, total=13.0),
        # Sprint with the values explicitly absent (None) — fallback
        # should be 0.0 (a float), not 0 (an int).
        {
            "id": "sprint-B",
            "name": "B",
            "state": "OPEN",
            "startAt": "2026-05-01T00:00:00Z",
            "endAt": "2026-05-15T00:00:00Z",
            "completedPoints": None,
            "totalPoints": None,
            "closedIssuesCount": 0,
        },
    ])
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sprints(ctx)
    a = next(s for s in out["sprints"] if s["name"] == "A")
    b = next(s for s in out["sprints"] if s["name"] == "B")
    assert a["completed_points"] == 5.5
    assert a["total_points"] == 13.0
    # The key behavioural assertion: a missing value is a FLOAT zero
    # (matches the docstring's typing), not an int zero.
    assert b["completed_points"] == 0.0
    assert isinstance(b["completed_points"], float)
    assert isinstance(b["total_points"], float)


# =============================================================================
# get_sprint_detail
# =============================================================================

def test_get_sprint_detail_by_name_case_insensitive():
    ctx = _ctx()
    responses = [
        # 1. _find_sprint_id calls list_sprints to walk sprints
        _sprints_page([
            _sprint_node("sprint-7", "Sprint 7"),
            _sprint_node("sprint-6", "Sprint 6", state="CLOSED",
                         start="2026-04-15T00:00:00Z",
                         end="2026-05-01T00:00:00Z",
                         completed=18.0, total=21.0, closed=9),
        ]),
        # 2. Sprint header
        _sprint_header_response(),
        # 3. Sprint issues (single page)
        _sprint_issues_page([
            _issue_node(100, title="Add token rotation",
                        assignees=["alice"]),
            _issue_node(101, title="Wire up refresh-token endpoint",
                        state="CLOSED", estimate=None,
                        assignees=[], pipeline=None),
        ]),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.get_sprint_detail(ctx, "sprint 7")
    assert out["ok"] is True
    assert out["sprint_id"] == "sprint-7"
    assert out["sprint_name"] == "Sprint 7"
    assert out["issue_count"] == 2
    assert out["issues"][0]["pipeline"] == "In Progress"
    assert out["issues"][0]["estimate"] == 3
    assert out["issues"][1]["state"] == "CLOSED"
    assert out["issues"][1]["estimate"] is None  # null preserved, not 0


def test_get_sprint_detail_current_alias():
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _sprint_header_response(),
        _sprint_issues_page([]),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.get_sprint_detail(ctx, "current")
    assert out["ok"] is True
    assert out["sprint_id"] == "sprint-7"


def test_get_sprint_detail_unknown_name():
    ctx = _ctx()
    with _patch_ctx_query(ctx, [_sprints_page([_sprint_node("sprint-7", "Sprint 7")])]):
        out = zh_graphql_ops.get_sprint_detail(ctx, "Sprint 99")
    assert out["ok"] is False
    assert "not found" in (out["error"] or "").lower()


def test_get_sprint_detail_empty_name_refused():
    """_find_sprint_id refuses empty-string names early (review note)."""
    ctx = _ctx()
    with _patch_ctx_query(ctx, [_sprints_page([_sprint_node("sprint-7", "Sprint 7")])]):
        out = zh_graphql_ops.get_sprint_detail(ctx, "")
    assert out["ok"] is False
    assert "non-empty" in (out["error"] or "").lower()


def test_get_current_sprint_no_active():
    """current/active with no active sprint surfaces a clear error."""
    ctx = _ctx()
    response = _sprints_page([], active_id=None, workspace_name="Quiet Team")
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.get_current_sprint(ctx)
    assert out["ok"] is False
    assert "no active sprint" in (out["error"] or "").lower()


def test_get_sprint_detail_walks_issues_pagination():
    """Review finding #4: sprints with >100 issues must paginate.

    Before the fix, the 101st issue was silently dropped.
    """
    ctx = _ctx()
    page_one = [_issue_node(n) for n in range(1, 101)]  # 100 issues
    page_two = [_issue_node(101, title="The hundred-and-first")]
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _sprint_header_response(),
        _sprint_issues_page(page_one, has_next=True, end_cursor="cursor-2"),
        _sprint_issues_page(page_two, has_next=False),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.get_sprint_detail(ctx, "current")
    assert out["issue_count"] == 101
    assert out["issues"][-1]["title"] == "The hundred-and-first"
    assert out["pagination_warning"] is None


# =============================================================================
# Sprint mutations (Bucket A): add_issues_to_sprint / remove_issues_from_sprint
# =============================================================================

def _issue_by_info_resp(number: int, *, owner: str = "acme",
                        repo_name: str = "widgets") -> dict:
    """Single-issue lookup stub (used by mutation pre-flight)."""
    return {
        "data": {
            "issueByInfo": {
                "id": f"issue-gid-{number}",
                "number": number,
                "title": f"Issue {number}",
                "state": "OPEN",
                "repository": {"ownerName": owner, "name": repo_name},
                "parentIssue": None,
            }
        }
    }


def _add_resp(linked_numbers: list[int], *, sprint_id: str = "sprint-7") -> dict:
    """`addIssuesToSprints` response wrapper.

    Builds one SprintIssue link per `linked_numbers` entry. Issues NOT
    in this list are inferred-failed by the production code.
    """
    return {
        "data": {
            "addIssuesToSprints": {
                "sprintIssues": [
                    {
                        "id": f"link-{n}-{sprint_id}",
                        "issue": {
                            "number": n,
                            "repository": {
                                "ownerName": "acme", "name": "widgets",
                            },
                        },
                        "sprint": {"id": sprint_id},
                    }
                    for n in linked_numbers
                ]
            }
        }
    }


def _remove_resp(still_attached_numbers: list[int],
                 *, sprint_id: str = "sprint-7") -> dict:
    """`removeIssuesFromSprints` response wrapper.

    `still_attached_numbers` is what remains in the sprint AFTER the
    mutation. The production code treats anything in this set that we
    asked to remove as inferred-failed.
    """
    return {
        "data": {
            "removeIssuesFromSprints": {
                "sprints": [
                    {
                        "id": sprint_id,
                        "sprintIssues": {
                            "nodes": [
                                {
                                    "issue": {
                                        "number": n,
                                        "repository": {
                                            "ownerName": "acme",
                                            "name": "widgets",
                                        },
                                    }
                                }
                                for n in still_attached_numbers
                            ],
                        },
                    }
                ]
            }
        }
    }


# --- add_issues_to_sprint -------------------------------------------------

def test_add_issues_to_sprint_happy_path():
    """All inputs come back as links → outcome=ok, succeeded=inputs, failed=[]."""
    ctx = _ctx()
    responses = [
        # 1. _find_sprint_id calls list_sprints
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        # 2-3. Pre-flight issueByInfo lookups
        _issue_by_info_resp(100),
        _issue_by_info_resp(101),
        # 4. Mutation response: both linked
        _add_resp([100, 101]),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_issues_to_sprint(ctx, "Sprint 7", [100, 101])
    assert out["ok"] is True
    assert out["outcome"] == "ok"
    assert sorted(out["succeeded"]) == [100, 101]
    assert out["failed"] == []
    assert out["success_count"] == 2
    assert out["failed_count"] == 0


def test_add_issues_to_sprint_partial_failure():
    """API only confirmed one of three — the other two are inferred-failed."""
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _issue_by_info_resp(100),
        _issue_by_info_resp(101),
        _issue_by_info_resp(102),
        _add_resp([101]),  # only #101 came back as linked
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_issues_to_sprint(
            ctx, "Sprint 7", [100, 101, 102]
        )
    assert out["ok"] is False
    assert out["outcome"] == "partial"
    assert out["succeeded"] == [101]
    # 100 and 102 absent from the response — inferred-failed.
    assert sorted(out["failed"]) == [100, 102]
    assert out["success_count"] == 1
    assert out["failed_count"] == 2


def test_add_issues_to_sprint_noop_when_all_already_linked():
    """Empty response (no new links created) → outcome=noop, ok=False.

    Common case: every input was already in the sprint. API silently
    accepts the mutation but creates no new links.
    """
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _issue_by_info_resp(100),
        _add_resp([]),  # zero new links
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_issues_to_sprint(ctx, "Sprint 7", [100])
    # Zero successes + zero failures is `noop`; classifier maps zero-
    # success-with-input-list-of-1 to "fail" because everything was
    # inferred-failed. Both noop and fail are non-ok — assert that.
    assert out["ok"] is False
    assert out["outcome"] in {"noop", "fail"}


def test_add_issues_to_sprint_sprint_not_found():
    """Sprint name lookup miss surfaces a clean error, no mutation fired."""
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_issues_to_sprint(ctx, "Sprint 99", [100])
    assert out["ok"] is False
    assert "not found" in (out["error"] or "").lower()


def test_add_issues_to_sprint_missing_issue_short_circuits():
    """Pre-flight: an unknown issue number is reported before firing."""
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _issue_by_info_resp(100),
        # 9999 returns no issue
        {"data": {"issueByInfo": None}},
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_issues_to_sprint(
            ctx, "Sprint 7", [100, 9999]
        )
    assert out["ok"] is False
    assert out["outcome"] == "fail"
    assert out["failed"] == [9999]


def test_add_issues_to_sprint_validates_inputs():
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.add_issues_to_sprint(ctx, "Sprint 7", [])
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.add_issues_to_sprint(ctx, "Sprint 7", [100, -5])


# --- remove_issues_from_sprint --------------------------------------------

def test_remove_issues_from_sprint_happy_path():
    """All inputs absent from post-state → outcome=ok."""
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _issue_by_info_resp(100),
        _issue_by_info_resp(101),
        # Post-state has neither 100 nor 101 — both were removed.
        _remove_resp([200, 201]),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_issues_from_sprint(
            ctx, "Sprint 7", [100, 101]
        )
    assert out["ok"] is True
    assert out["outcome"] == "ok"
    assert sorted(out["succeeded"]) == [100, 101]
    assert out["failed"] == []


def test_remove_issues_from_sprint_partial_failure():
    """One input still in the sprint post-mutation → inferred-failed."""
    ctx = _ctx()
    responses = [
        _sprints_page([_sprint_node("sprint-7", "Sprint 7")]),
        _issue_by_info_resp(100),
        _issue_by_info_resp(101),
        # 100 is gone, 101 stuck.
        _remove_resp([101, 999]),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_issues_from_sprint(
            ctx, "Sprint 7", [100, 101]
        )
    assert out["ok"] is False
    assert out["outcome"] == "partial"
    assert out["succeeded"] == [100]
    assert out["failed"] == [101]


def test_remove_issues_from_sprint_validates_inputs():
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.remove_issues_from_sprint(ctx, "Sprint 7", [])
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.remove_issues_from_sprint(ctx, "Sprint 7", [-1])
