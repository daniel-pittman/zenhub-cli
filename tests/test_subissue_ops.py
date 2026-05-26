"""Tests for sub-issue GraphQL operations.

These exercise the same regression cases the v1.5.0 text-contract review
surfaced — titles containing the U+2502 separator, titles containing
the ✓ closed marker, pipeline names containing em-dashes, multi-element
succeeded/failed sets, no-op outcomes, cross-repo children, case-
insensitive repo comparison, and pagination defenses — but against the
direct GraphQL implementation rather than the text-parsing layer.

Every test mocks `RepoContext.query` so no live ZenHub calls are made.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import zh_api
import zh_graphql_ops


# =============================================================================
# Helpers
# =============================================================================

def _ctx(owner_repo: str = "acme/widgets") -> zh_api.RepoContext:
    """Build a RepoContext skipping the network-bound resolution."""
    return zh_api.RepoContext(
        owner_repo=owner_repo,
        repo_id="repo-gid-123",
        workspace_id="workspace-gid-456",
        token="fake-token",
    )


def _patch_ctx_query(ctx: zh_api.RepoContext, responses: list[dict]):
    """Patch ctx.query to return each entry of `responses` in turn."""
    it = iter(responses)
    return patch.object(
        ctx, "query",
        side_effect=lambda query, variables=None: next(it),
    )


# =============================================================================
# list_sub_issues
# =============================================================================

def test_list_sub_issues_handles_pipe_in_title_and_check_in_state():
    """Titles containing │ or ✓ are returned untruncated and unmolested.

    These were two of the recurring failures of the v1.5.0 text contract:
    the visual table's "│" separator was indistinguishable from a "│" in
    the title, and the ✓ marker for CLOSED state lived in the same column
    as the title prefix. Direct GraphQL doesn't have this problem; this
    test exists so a future refactor that re-introduces text parsing
    will get caught.
    """
    ctx = _ctx()
    weird_title = "Refactor X | rebuild Y │ verify ✓ all green"
    response = {
        "data": {
            "issueByInfo": {
                "number": 42,
                "title": "Parent task",
                "state": "OPEN",
                "zenhubChildIssues": {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 100,
                            "title": weird_title,
                            "state": "CLOSED",
                            "assignees": {"nodes": [{"login": "alice"}]},
                            "pipelineIssue": {
                                "pipeline": {"name": "In Review"}
                            },
                            "repository": {
                                "ownerName": "acme",
                                "name": "widgets",
                            },
                        }
                    ],
                },
            }
        }
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sub_issues(ctx, 42)
    assert out["ok"] is True
    assert out["children"][0]["title"] == weird_title  # untruncated, unmolested
    assert out["children"][0]["state"] == "CLOSED"


def test_list_sub_issues_em_dash_pipeline_kept_literal():
    """Em-dash in a pipeline name is preserved verbatim.

    v1.5.0 used "—" as a sentinel meaning "no pipeline". That collided
    with literal-em-dash pipeline names. The new contract returns the
    raw string from the API and uses `None` for absent pipelines.
    """
    ctx = _ctx()
    response = {
        "data": {
            "issueByInfo": {
                "number": 42,
                "title": "Parent",
                "state": "OPEN",
                "zenhubChildIssues": {
                    "totalCount": 2,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 101,
                            "title": "Has em-dash pipeline",
                            "state": "OPEN",
                            "assignees": {"nodes": []},
                            "pipelineIssue": {
                                "pipeline": {"name": "Phase 1 — Discovery"}
                            },
                            "repository": {
                                "ownerName": "acme", "name": "widgets"},
                        },
                        {
                            "number": 102,
                            "title": "No pipeline",
                            "state": "OPEN",
                            "assignees": {"nodes": []},
                            "pipelineIssue": None,
                            "repository": {
                                "ownerName": "acme", "name": "widgets"},
                        },
                    ],
                },
            }
        }
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sub_issues(ctx, 42)
    children = out["children"]
    assert children[0]["pipeline"] == "Phase 1 — Discovery"
    assert children[1]["pipeline"] is None


def test_list_sub_issues_walks_pagination():
    """Multi-page response is walked until hasNextPage=false."""
    ctx = _ctx()

    def _page(nodes, has_next, cursor):
        return {
            "data": {
                "issueByInfo": {
                    "number": 42,
                    "title": "P",
                    "state": "OPEN",
                    "zenhubChildIssues": {
                        "totalCount": 3,
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": cursor,
                        },
                        "nodes": nodes,
                    },
                }
            }
        }

    def _node(n: int) -> dict:
        return {
            "number": n,
            "title": f"#{n}",
            "state": "OPEN",
            "assignees": {"nodes": []},
            "pipelineIssue": None,
            "repository": {"ownerName": "acme", "name": "widgets"},
        }

    with _patch_ctx_query(ctx, [
        _page([_node(100), _node(101)], True, "cursor1"),
        _page([_node(102)], False, None),
    ]):
        out = zh_graphql_ops.list_sub_issues(ctx, 42)
    assert out["fetched_count"] == 3
    assert [c["number"] for c in out["children"]] == [100, 101, 102]
    assert out["pagination_warning"] is None


def test_list_sub_issues_stuck_cursor_breaks_walk():
    """If the server keeps returning the same endCursor, we bail.

    This is one of the surviving logic-level findings from the v1.5.0
    review: a stuck cursor would otherwise loop forever.
    """
    ctx = _ctx()
    stuck = {
        "data": {
            "issueByInfo": {
                "number": 42,
                "title": "P",
                "state": "OPEN",
                "zenhubChildIssues": {
                    "totalCount": 100,
                    "pageInfo": {
                        "hasNextPage": True,
                        "endCursor": "SAME_CURSOR",
                    },
                    "nodes": [
                        {
                            "number": 100, "title": "A", "state": "OPEN",
                            "assignees": {"nodes": []},
                            "pipelineIssue": None,
                            "repository": {
                                "ownerName": "acme", "name": "widgets"},
                        }
                    ],
                },
            }
        }
    }
    # Three "same cursor" responses; the walker should detect on the
    # second iteration.
    with _patch_ctx_query(ctx, [stuck, stuck, stuck]):
        out = zh_graphql_ops.list_sub_issues(ctx, 42)
    assert "cursor unchanged" in (out["pagination_warning"] or "").lower()


def test_list_sub_issues_iteration_cap_belt_and_suspenders():
    """If the cursor changes every page but never terminates, the cap fires."""
    ctx = _ctx()

    def _page(i: int) -> dict:
        return {
            "data": {
                "issueByInfo": {
                    "number": 42,
                    "title": "P",
                    "state": "OPEN",
                    "zenhubChildIssues": {
                        "totalCount": 1_000_000,
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": f"cursor-{i}",
                        },
                        "nodes": [
                            {
                                "number": 1000 + i,
                                "title": f"x{i}",
                                "state": "OPEN",
                                "assignees": {"nodes": []},
                                "pipelineIssue": None,
                                "repository": {
                                    "ownerName": "acme", "name": "widgets"},
                            }
                        ],
                    },
                }
            }
        }

    # Lower the cap so the test runs in finite time.
    cap = zh_graphql_ops.MAX_PAGINATION_ITERATIONS
    try:
        zh_graphql_ops.MAX_PAGINATION_ITERATIONS = 5
        with _patch_ctx_query(ctx, [_page(i) for i in range(20)]):
            out = zh_graphql_ops.list_sub_issues(ctx, 42)
    finally:
        zh_graphql_ops.MAX_PAGINATION_ITERATIONS = cap
    assert (
        out["pagination_warning"] is not None
        and "iteration cap" in out["pagination_warning"].lower()
    )


def test_list_sub_issues_returns_repository_per_child():
    """Cross-repo workspaces: every child carries its `repository.{owner,name}`.

    Round-3 finding #5: a multi-repo workspace can have two of a parent's
    children sharing an issue number (one per repo). The MCP wrapper
    surfaces the owning repo so callers can disambiguate.
    """
    ctx = _ctx()
    response = {
        "data": {
            "issueByInfo": {
                "number": 42,
                "title": "P",
                "state": "OPEN",
                "zenhubChildIssues": {
                    "totalCount": 2,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 100, "title": "Local", "state": "OPEN",
                            "assignees": {"nodes": []},
                            "pipelineIssue": None,
                            "repository": {
                                "ownerName": "acme", "name": "widgets"},
                        },
                        {
                            "number": 100, "title": "Other repo", "state": "OPEN",
                            "assignees": {"nodes": []},
                            "pipelineIssue": None,
                            "repository": {
                                "ownerName": "acme", "name": "OTHER"},
                        },
                    ],
                },
            }
        }
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sub_issues(ctx, 42)
    repos = {c["repository"]["name"] for c in out["children"]}
    assert repos == {"widgets", "OTHER"}


# =============================================================================
# repos_match — case-insensitive comparison (carried-forward finding)
# =============================================================================

def test_repos_match_case_insensitive():
    """Owner/repo comparison must be case-insensitive.

    Round-4 finding #4: the bash version had a case-sensitive comparison
    that broke mixed-case git remotes. The Python port must NOT
    reproduce that bug.
    """
    assert zh_api.repos_match(
        {"ownerName": "Acme", "name": "Widgets"}, "acme/widgets"
    )
    assert zh_api.repos_match(
        {"ownerName": "acme", "name": "widgets"}, "Acme/WIDGETS"
    )
    assert not zh_api.repos_match(
        {"ownerName": "acme", "name": "other"}, "acme/widgets"
    )
    assert not zh_api.repos_match(None, "acme/widgets")


# =============================================================================
# add_sub_issues
# =============================================================================

def _issue_by_info(number: int, *, parent: dict | None = None,
                   owner_repo: str = "acme/widgets") -> dict:
    """Build a stub issueByInfo response."""
    owner, _, name = owner_repo.partition("/")
    return {
        "data": {
            "issueByInfo": {
                "id": f"issue-gid-{number}",
                "number": number,
                "title": f"Issue {number}",
                "state": "OPEN",
                "repository": {"ownerName": owner, "name": name},
                "parentIssue": parent,
            }
        }
    }


def test_add_sub_issues_partial_failure_split():
    """Partial-failure case returns split succeeded/failed sets.

    v1.5.0 returned the raw input list as `added` on success and the raw
    input list as `removed` on a partial failure too — both lies. The
    new contract sources both arrays from the API's payload.
    """
    ctx = _ctx()
    # parent lookup + 3 child lookups + 1 add mutation
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        _issue_by_info(102),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 2,
                    "failedIssues": [
                        {
                            "number": 102,
                            "repository": {
                                "ownerName": "acme", "name": "widgets"},
                        }
                    ],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101, 102])
    assert out["outcome"] == "partial"
    assert out["ok"] is False
    assert out["success_count"] == 2
    assert out["failed_count"] == 1
    assert sorted(out["succeeded"]) == [100, 101]
    assert [f["number"] for f in out["failed"]] == [102]


def test_add_sub_issues_noop_outcome_is_not_ok():
    """successCount=0, failedCount=0 is noop with ok=False.

    Round-3 finding #2: the API silently no-ops if every requested child
    is already linked. Must NOT look like a successful add.
    """
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 0,
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    assert out["outcome"] == "noop"
    assert out["ok"] is False
    assert out["succeeded"] == []
    assert out["failed"] == []


def test_add_sub_issues_validates_numeric_input():
    """Non-positive-int child numbers raise before any network call.

    Logic-level carried-forward finding: numeric format validation
    up-front means clean errors instead of opaque GraphQL crashes.
    """
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.add_sub_issues(ctx, 42, [100, "not-int"])  # type: ignore[arg-type]
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.add_sub_issues(ctx, 42, [100, -5])


# =============================================================================
# remove_sub_issues
# =============================================================================

def test_remove_sub_issues_wrong_parent_preflight():
    """Pre-flight catches children whose actual parent is not us.

    The API's removeSubIssues takes only child IDs and unlinks each
    from its real parent; without our pre-flight, a typo in the parent
    arg would unlink the wrong sibling set while silently reporting
    success.
    """
    ctx = _ctx()
    # Parent lookup, then three children: two with correct parent #42,
    # one with parent #999.
    correct_parent = {
        "id": "issue-gid-42",
        "number": 42,
        "title": "Parent",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    responses = [
        _issue_by_info(42),
        _issue_by_info(100, parent=correct_parent),
        _issue_by_info(101, parent={
            **correct_parent, "id": "issue-gid-999", "number": 999,
        }),  # wrong parent
        _issue_by_info(102, parent=correct_parent),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101, 102])
    assert out["ok"] is False
    assert out["outcome"] == "fail"
    assert "wrong parent" in (out.get("error") or "").lower()
    # Mid-loop validation accumulator: every mismatch is reported, not
    # just the first one.
    nums = {f["number"] for f in out["failed"]}
    # We expect at least the wrong-parent child to be listed.
    assert 101 in nums


def test_remove_sub_issues_cross_repo_caught():
    """A child in a different repo than the cwd is rejected pre-flight.

    Carried-forward finding: cross-repo silent wrong-target check.
    """
    ctx = _ctx(owner_repo="acme/widgets")
    correct_parent = {
        "id": "issue-gid-42",
        "number": 42,
        "title": "Parent",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    responses = [
        _issue_by_info(42),
        # child lives in acme/OTHER
        _issue_by_info(100, parent=correct_parent, owner_repo="acme/OTHER"),
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100])
    assert out["ok"] is False
    assert "cross-repo" in (out.get("error") or "").lower()


def test_remove_sub_issues_happy_path():
    ctx = _ctx()
    correct_parent = {
        "id": "issue-gid-42",
        "number": 42,
        "title": "Parent",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    responses = [
        _issue_by_info(42),
        _issue_by_info(100, parent=correct_parent),
        _issue_by_info(101, parent=correct_parent),
        {
            "data": {
                "removeSubIssues": {
                    "successCount": 2,
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101])
    assert out["ok"] is True
    assert out["outcome"] == "ok"
    assert sorted(out["succeeded"]) == [100, 101]


# =============================================================================
# reorder_sub_issue
# =============================================================================

def test_reorder_sub_issue_only_child_is_noop():
    """Only-child reorder: outcome=noop, no mutation fired, ok=False.

    Round-3 finding #3.
    """
    ctx = _ctx()
    parent_info = {
        "id": "issue-gid-42",
        "number": 42,
        "title": "Parent",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    # child lookup + listing showing only the child
    responses = [
        _issue_by_info(100, parent=parent_info),
        {
            "data": {
                "issueByInfo": {
                    "number": 42,
                    "title": "Parent",
                    "state": "OPEN",
                    "zenhubChildIssues": {
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "number": 100,
                                "title": "Only child",
                                "state": "OPEN",
                                "assignees": {"nodes": []},
                                "pipelineIssue": None,
                                "repository": {
                                    "ownerName": "acme", "name": "widgets",
                                },
                            }
                        ],
                    },
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
    assert out["ok"] is False
    assert out["outcome"] == "noop"


def test_reorder_sub_issue_rejects_invalid_position():
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.reorder_sub_issue(ctx, 100, "middle")


def test_reorder_sub_issue_after_requires_sibling():
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.reorder_sub_issue(ctx, 100, "after")
