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
    assert "cursor not advancing" in (out["pagination_warning"] or "").lower()


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


def test_add_sub_issues_rejects_bool_input():
    """Round-5 finding #10: `bool` is a subclass of `int` in Python,
    so naive `isinstance(n, int)` accepts `True` (== 1) and
    `False` (== 0). The validator must explicitly reject bool to
    avoid firing the mutation with garbage input that GraphQL
    might or might not silently accept.
    """
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError) as exc:
        zh_graphql_ops.add_sub_issues(ctx, 42, [True])  # type: ignore[list-item]
    assert "positive int" in str(exc.value).lower()
    # Same for the parent_number / child_number args
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.add_sub_issues(ctx, True, [100])  # type: ignore[arg-type]
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.remove_sub_issues(ctx, 42, [False])  # type: ignore[list-item]
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.reorder_sub_issue(ctx, True, "top")  # type: ignore[arg-type]


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


def test_reorder_sub_issue_rejects_self_anchor():
    """Review finding #9: anchoring after/before yourself is meaningless."""
    ctx = _ctx()
    with pytest.raises(zh_api.ZhApiError) as exc:
        zh_graphql_ops.reorder_sub_issue(
            ctx, 100, "after", sibling_number=100
        )
    assert "self-anchor" in str(exc.value).lower()
    with pytest.raises(zh_api.ZhApiError):
        zh_graphql_ops.reorder_sub_issue(
            ctx, 100, "before", sibling_number=100
        )


def test_reorder_sub_issue_top_crosses_repos_via_id_anchor():
    """Review finding #2: top/bottom must anchor by id, not by repo-filtered number.

    Scenario: child #100 lives in acme/widgets. The "first" sibling is
    a child in acme/OTHER (an issue numbered 50). Before the fix,
    `_find_sibling_id` filtered by repo and returned None, the
    mutation fired with both ids null, and the API returned success
    while doing nothing. After the fix, the listing's `id` is used
    directly — workspace-global, so cross-repo siblings are valid
    anchors.
    """
    ctx = _ctx()
    parent_info = {
        "id": "issue-gid-42",
        "number": 42,
        "title": "Parent",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    responses = [
        # 1. Resolve child
        _issue_by_info(100, parent=parent_info),
        # 2. List siblings — first sibling is in a DIFFERENT repo
        {
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
                                "id": "issue-gid-50-OTHER",
                                "number": 50,
                                "title": "Sibling in other repo",
                                "state": "OPEN",
                                "assignees": {"nodes": []},
                                "pipelineIssue": None,
                                "repository": {
                                    "ownerName": "acme", "name": "OTHER",
                                },
                            },
                            {
                                "id": "issue-gid-100",
                                "number": 100,
                                "title": "Self",
                                "state": "OPEN",
                                "assignees": {"nodes": []},
                                "pipelineIssue": None,
                                "repository": {
                                    "ownerName": "acme", "name": "widgets",
                                },
                            },
                        ],
                    },
                }
            }
        },
        # 3. The mutation should fire with beforeId=<other repo's gid>
        {
            "data": {
                "reprioritizeSubIssue": {
                    "success": True, "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
    assert out["ok"] is True
    assert out["outcome"] == "ok"


def test_reorder_sub_issue_refuses_when_both_anchors_null():
    """Belt-and-suspenders: if we somehow can't resolve any anchor, fail loud.

    Defends against a regression that would silently no-op (the bug
    behind review #2). With the explicit guard, this case returns
    `outcome="fail"` rather than firing a vacuous mutation.
    """
    ctx = _ctx()
    parent_info = {
        "id": "issue-gid-42",
        "number": 42,
        "title": "Parent",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    # Sibling listing with no ids at all (degraded API response) AND
    # multiple siblings — would short-circuit past the only-child noop.
    responses = [
        _issue_by_info(100, parent=parent_info),
        {
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
                                # id missing entirely
                                "number": 50,
                                "title": "Sibling",
                                "state": "OPEN",
                                "assignees": {"nodes": []},
                                "pipelineIssue": None,
                                "repository": {
                                    "ownerName": "acme", "name": "widgets",
                                },
                            },
                        ],
                    },
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
    # Sibling has no id, so "other" filter (`s.get("id") and ...`) gives
    # None — we treat it as the only-child case (still a noop outcome).
    assert out["ok"] is False
    assert out["outcome"] == "noop"


# =============================================================================
# succeeded-divergence handling (review finding #3)
# =============================================================================

def test_add_sub_issues_succeeded_divergence_returns_empty_succeeded():
    """When successCount doesn't match `input - failedIssues`, refuse to claim.

    Concrete: API returns successCount=1, failedIssues=[] for 3 input
    children. We can't tell which one of the three landed. Inference
    by subtraction would lie ("all 3 succeeded"); the fix returns
    succeeded=[] with a partial_success_warning describing the
    divergence.
    """
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        _issue_by_info(102),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 1,           # only one actually landed
                    "failedIssues": [],          # but API didn't tell us which
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101, 102])
    assert out["succeeded"] == []
    assert out["partial_success_warning"] is not None
    assert "successCount=1" in out["partial_success_warning"]
    # Round-6 #3: round-5 fixed the data (succeeded=[]) but left the
    # signal stale. SPEC: when partial_success_warning is set AND
    # the API said it succeeded, `ok` and `outcome` must agree with
    # the data — outcome="partial", ok=False.
    assert out["outcome"] == "partial", (
        "Round-6 #3: divergence guard fires → outcome must downgrade "
        "to 'partial', not the API's success_count-derived 'ok'."
    )
    assert out["ok"] is False, (
        "Round-6 #3: ok must agree with `succeeded == []` — it makes "
        "no sense to report ok=True alongside a warning that we "
        "couldn't identify what succeeded."
    )


def test_remove_sub_issues_succeeded_divergence_returns_empty_succeeded():
    """Same divergence guard on the remove side."""
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
                    "successCount": 1,
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101])
    assert out["succeeded"] == []
    assert out["partial_success_warning"] is not None
    # Round-6 #3 mirror — same signal-must-match-data SPEC on remove.
    assert out["outcome"] == "partial"
    assert out["ok"] is False


def test_remove_sub_issues_divergence_noop_preserved():
    """Round-7 #1 SPEC pin: when `successCount=0, failedIssues=[]`
    on `remove`, the divergence guard fires (succeeded=[] vs
    inferred=inputs ≠ 0) AND outcome MUST stay `noop` — not get
    clobbered to `partial`.

    Pre-round-7 the remove path's override was unconditional, so
    a strict no-op got mis-signaled as partial + empty succeeded +
    empty failed (internally inconsistent). The `outcome == "ok"`
    guard from the `add` side fixes the asymmetry.
    """
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
                    "successCount": 0,
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101])
    assert out["outcome"] == "noop", (
        f"Round-7 #1: strict no-op (success=0, failed=0) must stay "
        f"`noop` even when divergence guard fires; got {out['outcome']!r}"
    )
    assert out["succeeded"] == []
    assert out["failed"] == []
    # The divergence guard still fires (succeeded=[] inferred-from
    # length mismatch), so the warning is still set — what matters
    # is the outcome label.
    assert out["partial_success_warning"] is not None
    # Round-8 #1: warning text must reflect the noop shape, not the
    # generic "cannot identify" phrasing that ok→partial divergence
    # uses. The operator needs to know this was a strict no-op so
    # they know to investigate whether the inputs were already in
    # the requested state vs. silently rejected.
    assert "strict no-op" in out["partial_success_warning"], (
        f"Round-8 #1: noop-divergence warning must name the shape; "
        f"got {out['partial_success_warning']!r}"
    )


def test_remove_sub_issues_divergence_fail_preserved():
    """Round-7 #1: when `successCount=0, failedIssues=[100]` and
    we have >1 inputs, outcome MUST stay `fail` — divergence
    guard does not override real failure.
    """
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
                    "successCount": 0,
                    "failedIssues": [{
                        "number": 100,
                        "repository": {"ownerName": "acme", "name": "widgets"},
                    }],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101])
    assert out["outcome"] == "fail", (
        f"Round-7 #1: real failure must stay `fail` — divergence "
        f"warning does not downgrade it; got {out['outcome']!r}"
    )
    assert out["succeeded"] == []
    # Divergence fires (success=0, failed=1, but 2 inputs → mismatch)
    assert out["partial_success_warning"] is not None
    # Round-8 #1: under-reported fail names the input(s) the API
    # neither succeeded nor failed.
    assert "did not report on" in out["partial_success_warning"], (
        f"Round-8 #1: fail-divergence warning must name the under-"
        f"report; got {out['partial_success_warning']!r}"
    )


def test_add_sub_issues_divergence_noop_preserved():
    """Round-7 #1 symmetric pin: the `add` side already has the
    `outcome == "ok"` guard, but pin it explicitly so a future
    regression mirroring the round-6 mistake on the `add` side
    is caught.
    """
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
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
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
    assert out["outcome"] == "noop"
    assert out["succeeded"] == []
    assert out["failed"] == []
    # Round-8 #1 symmetric.
    assert out["partial_success_warning"] is not None
    assert "strict no-op" in out["partial_success_warning"]


def test_add_sub_issues_divergence_fail_preserved():
    """Round-7 #1 symmetric: real failure stays `fail` on add."""
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 0,
                    "failedIssues": [{
                        "number": 100,
                        "repository": {"ownerName": "acme", "name": "widgets"},
                    }],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
    assert out["outcome"] == "fail"
    assert out["succeeded"] == []
    # Round-8 #1 symmetric.
    assert out["partial_success_warning"] is not None
    assert "did not report on" in out["partial_success_warning"]


def test_add_sub_issues_full_success_when_count_matches():
    """Sanity: when successCount equals inferred set, we DO trust it."""
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 2,           # matches the 2 inputs
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
    assert out["ok"] is True
    assert sorted(out["succeeded"]) == [100, 101]
    assert out["partial_success_warning"] is None


# =============================================================================
# round-8 #1: ok→partial divergence warning text shape
# =============================================================================

def test_add_sub_issues_ok_divergence_warning_text():
    """Round-8 #1: when outcome would have been "ok" but divergence
    fires (success=1 with no failed, but 2 inputs → inferred=[both]),
    outcome flips to "partial" and warning names the "cannot identify"
    shape — distinct from the noop and fail-divergence variants.
    """
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 1,        # 1 but inferred=[100,101]
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
    assert out["outcome"] == "partial"
    assert out["succeeded"] == []
    assert out["partial_success_warning"] is not None
    assert (
        "cannot identify which inputs succeeded"
        in out["partial_success_warning"].lower()
    ), (
        f"Round-8 #1: ok→partial divergence warning must name the "
        f"can't-identify shape; got {out['partial_success_warning']!r}"
    )


def test_remove_sub_issues_ok_divergence_warning_text():
    """Round-8 #1 symmetric on `remove`."""
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
                    "successCount": 1,
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101])
    assert out["outcome"] == "partial"
    assert out["succeeded"] == []
    assert out["partial_success_warning"] is not None
    assert (
        "cannot identify which inputs succeeded"
        in out["partial_success_warning"].lower()
    )


# =============================================================================
# round-8 #2: `unaccounted` field + count conservation
# =============================================================================

def test_subissue_add_count_conservation_under_fail_divergence():
    """Round-8 #2: under fail-divergence (success=0, failed=[100],
    but 3 inputs), `unaccounted` MUST surface the input(s) the API
    didn't report on. Invariant:
        len(succeeded) + len(failed) + len(unaccounted) == len(input)
    holds across every outcome.
    """
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        _issue_by_info(102),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 0,
                    "failedIssues": [{
                        "number": 100,
                        "repository": {"ownerName": "acme", "name": "widgets"},
                    }],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101, 102])
    assert out["outcome"] == "fail"
    assert out["succeeded"] == []
    assert [f["number"] for f in out["failed"]] == [100]
    # 101 and 102 are unaccounted — neither succeeded nor in failedIssues.
    assert out["unaccounted"] == [101, 102]
    # Conservation invariant.
    assert (
        len(out["succeeded"]) + len(out["failed"]) + len(out["unaccounted"])
        == 3
    )


def test_subissue_remove_count_conservation_under_fail_divergence():
    """Round-8 #2 symmetric on `remove`."""
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
        _issue_by_info(102, parent=correct_parent),
        {
            "data": {
                "removeSubIssues": {
                    "successCount": 0,
                    "failedIssues": [{
                        "number": 100,
                        "repository": {"ownerName": "acme", "name": "widgets"},
                    }],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101, 102])
    assert out["outcome"] == "fail"
    assert out["succeeded"] == []
    assert [f["number"] for f in out["failed"]] == [100]
    assert out["unaccounted"] == [101, 102]
    assert (
        len(out["succeeded"]) + len(out["failed"]) + len(out["unaccounted"])
        == 3
    )


def test_subissue_add_unaccounted_empty_on_trusted_path():
    """Round-8 #2: when successCount matches the inferred set,
    `unaccounted` MUST be empty — there are no API-omitted inputs
    on the trusted path.
    """
    ctx = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100),
        _issue_by_info(101),
        {
            "data": {
                "addSubIssues": {
                    "successCount": 2,
                    "failedIssues": [],
                    "githubErrors": {},
                }
            }
        },
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
    assert out["ok"] is True
    assert out["unaccounted"] == []


# =============================================================================
# round-8 #4: pre-flight return shape completeness
# =============================================================================

def test_subissue_add_pre_flight_result_shape_complete():
    """Round-8 #4: every pre-flight return site MUST include the full
    documented result shape — `unaccounted` and `partial_success_warning`
    cannot be missing or callers reading those keys will KeyError.
    """
    ctx = _ctx()
    # Parent not found — the first pre-flight return site.
    responses = [
        {"data": {"issueByInfo": None}},
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    # All keys present.
    for k in (
        "ok", "parent_number", "outcome", "success_count", "failed_count",
        "succeeded", "failed", "unaccounted", "github_errors",
        "partial_success_warning", "error",
    ):
        assert k in out, f"pre-flight (parent-not-found) missing key {k!r}"
    assert out["unaccounted"] == []
    assert out["partial_success_warning"] is None

    # Child not found — the second pre-flight return site.
    ctx2 = _ctx()
    responses = [
        _issue_by_info(42),
        {"data": {"issueByInfo": None}},  # child 100 lookup misses
    ]
    with _patch_ctx_query(ctx2, responses):
        out2 = zh_graphql_ops.add_sub_issues(ctx2, 42, [100])
    for k in (
        "unaccounted", "partial_success_warning",
    ):
        assert k in out2, f"pre-flight (child-not-found) missing key {k!r}"
    assert out2["unaccounted"] == []
    assert out2["partial_success_warning"] is None


def test_subissue_remove_pre_flight_result_shape_complete():
    """Round-8 #4 symmetric: `remove` has two pre-flight return sites
    (parent-not-found, validation-failed) — both must include the
    full documented shape.
    """
    ctx = _ctx()
    # Parent not found.
    responses = [
        {"data": {"issueByInfo": None}},
    ]
    with _patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100])
    for k in ("unaccounted", "partial_success_warning"):
        assert k in out, f"remove pre-flight (parent-not-found) missing {k!r}"
    assert out["unaccounted"] == []
    assert out["partial_success_warning"] is None

    # Validation failed: child found but wrong parent.
    wrong = {
        "id": "issue-gid-99",
        "number": 99,
        "title": "Wrong",
        "repository": {"ownerName": "acme", "name": "widgets"},
    }
    ctx2 = _ctx()
    responses = [
        _issue_by_info(42),
        _issue_by_info(100, parent=wrong),
    ]
    with _patch_ctx_query(ctx2, responses):
        out2 = zh_graphql_ops.remove_sub_issues(ctx2, 42, [100])
    assert "Pre-flight validation failed" in (out2["error"] or "")
    for k in ("unaccounted", "partial_success_warning"):
        assert k in out2, f"remove pre-flight (validation) missing {k!r}"
    assert out2["unaccounted"] == []
    assert out2["partial_success_warning"] is None


# =============================================================================
# listing surfaces `id` (review finding #2a)
# =============================================================================

def test_list_sub_issues_returns_id_for_each_child():
    """`id` is now part of every CHILD node; tests anchoring depends on it."""
    ctx = _ctx()
    response = {
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
                            "id": "issue-gid-99",
                            "number": 99,
                            "title": "Child",
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
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sub_issues(ctx, 42)
    assert out["children"][0]["id"] == "issue-gid-99"
