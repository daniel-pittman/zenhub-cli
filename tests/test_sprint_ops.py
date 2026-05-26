"""Tests for sprint GraphQL operations.

Sprint functionality inspired by the design proposed in PR #2 by
@jeremiahrose; these tests exercise the Python rewrite against the
same workspace/Sprint GraphQL types that proposal used.
"""

from __future__ import annotations

from unittest.mock import patch

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
    it = iter(responses)
    return patch.object(
        ctx, "query",
        side_effect=lambda query, variables=None: next(it),
    )


# =============================================================================
# list_sprints
# =============================================================================

def test_list_sprints_open_only_marks_active():
    """The active sprint's id is marked with is_active=True."""
    ctx = _ctx()
    response = {
        "data": {
            "workspace": {
                "id": "workspace-gid-456",
                "name": "Backend Team",
                "activeSprint": {"id": "sprint-7", "name": "Sprint 7"},
                "sprints": {
                    "nodes": [
                        {
                            "id": "sprint-7",
                            "name": "Sprint 7",
                            "state": "OPEN",
                            "startAt": "2026-05-01T00:00:00Z",
                            "endAt": "2026-05-15T00:00:00Z",
                            "completedPoints": 5.0,
                            "totalPoints": 13.0,
                            "closedIssuesCount": 3,
                        },
                        {
                            "id": "sprint-8",
                            "name": "Sprint 8",
                            "state": "OPEN",
                            "startAt": "2026-05-15T00:00:00Z",
                            "endAt": "2026-05-29T00:00:00Z",
                            "completedPoints": 0,
                            "totalPoints": 0,
                            "closedIssuesCount": 0,
                        },
                    ]
                },
            }
        }
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sprints(ctx)
    assert out["ok"] is True
    assert out["workspace_name"] == "Backend Team"
    assert out["active_sprint_id"] == "sprint-7"
    names_active = {(s["name"], s["is_active"]) for s in out["sprints"]}
    assert names_active == {("Sprint 7", True), ("Sprint 8", False)}


def test_list_sprints_handles_empty_workspace():
    ctx = _ctx()
    response = {
        "data": {
            "workspace": {
                "id": "workspace-gid-456",
                "name": "Empty Team",
                "activeSprint": None,
                "sprints": {"nodes": []},
            }
        }
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.list_sprints(ctx)
    assert out["ok"] is True
    assert out["sprints"] == []
    assert out["active_sprint_id"] is None


# =============================================================================
# get_sprint_detail
# =============================================================================

_SPRINTS_INDEX = {
    "data": {
        "workspace": {
            "id": "workspace-gid-456",
            "name": "Backend Team",
            "activeSprint": {"id": "sprint-7", "name": "Sprint 7"},
            "sprints": {
                "nodes": [
                    {
                        "id": "sprint-7",
                        "name": "Sprint 7",
                        "state": "OPEN",
                        "startAt": "2026-05-01T00:00:00Z",
                        "endAt": "2026-05-15T00:00:00Z",
                        "completedPoints": 5,
                        "totalPoints": 13,
                        "closedIssuesCount": 3,
                    },
                    {
                        "id": "sprint-6",
                        "name": "Sprint 6",
                        "state": "CLOSED",
                        "startAt": "2026-04-15T00:00:00Z",
                        "endAt": "2026-05-01T00:00:00Z",
                        "completedPoints": 18,
                        "totalPoints": 21,
                        "closedIssuesCount": 9,
                    },
                ]
            },
        }
    }
}


def _sprint_detail_node() -> dict:
    return {
        "data": {
            "node": {
                "id": "sprint-7",
                "name": "Sprint 7",
                "description": "Stabilize the auth refactor",
                "state": "OPEN",
                "startAt": "2026-05-01T00:00:00Z",
                "endAt": "2026-05-15T00:00:00Z",
                "completedPoints": 5,
                "totalPoints": 13,
                "closedIssuesCount": 3,
                "sprintIssues": {
                    "nodes": [
                        {
                            "issue": {
                                "number": 100,
                                "title": "Add token rotation",
                                "state": "OPEN",
                                "htmlUrl": "https://github.com/acme/widgets/issues/100",
                                "estimate": {"value": 3},
                                "assignees": {
                                    "nodes": [{"login": "alice"}]
                                },
                                "repository": {
                                    "ownerName": "acme", "name": "widgets",
                                },
                                "pipelineIssues": {
                                    "nodes": [
                                        {"pipeline": {"name": "In Progress"}}
                                    ]
                                },
                            }
                        },
                        {
                            "issue": {
                                "number": 101,
                                "title": "Wire up refresh-token endpoint",
                                "state": "CLOSED",
                                "htmlUrl": "https://github.com/acme/widgets/issues/101",
                                "estimate": None,
                                "assignees": {"nodes": []},
                                "repository": {
                                    "ownerName": "acme", "name": "widgets",
                                },
                                "pipelineIssues": {"nodes": []},
                            }
                        },
                    ]
                },
            }
        }
    }


def test_get_sprint_detail_by_name_case_insensitive():
    ctx = _ctx()
    with _patch_ctx_query(ctx, [_SPRINTS_INDEX, _sprint_detail_node()]):
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
    with _patch_ctx_query(ctx, [_SPRINTS_INDEX, _sprint_detail_node()]):
        out = zh_graphql_ops.get_sprint_detail(ctx, "current")
    assert out["ok"] is True
    assert out["sprint_id"] == "sprint-7"


def test_get_sprint_detail_unknown_name():
    ctx = _ctx()
    with _patch_ctx_query(ctx, [_SPRINTS_INDEX]):
        out = zh_graphql_ops.get_sprint_detail(ctx, "Sprint 99")
    assert out["ok"] is False
    assert "not found" in (out["error"] or "").lower()


def test_get_current_sprint_no_active():
    """current/active with no active sprint surfaces a clear error."""
    ctx = _ctx()
    response = {
        "data": {
            "workspace": {
                "id": "workspace-gid-456",
                "name": "Quiet Team",
                "activeSprint": None,
                "sprints": {"nodes": []},
            }
        }
    }
    with _patch_ctx_query(ctx, [response]):
        out = zh_graphql_ops.get_current_sprint(ctx)
    assert out["ok"] is False
    assert "no active sprint" in (out["error"] or "").lower()
