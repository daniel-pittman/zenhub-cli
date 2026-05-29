"""Shared GraphQL response fixtures for the test matrix.

Every verb in `zh_graphql_ops` is exercised against a standard battery
of scenarios (happy / empty-input / partial-fail / null-node / multi-
repo / pagination-edges / dup-input / self-anchor). These fixture
builders produce the response shapes each scenario uses. Sharing
them keeps the matrix tests honest — fixture drift is itself a class
of bug the matrix is meant to catch.

Each builder is small and named after the GraphQL shape it produces
(not the scenario it serves). The matrix tests compose them by
threading a list of responses through `_patch_ctx_query` in the
order the verb is expected to call `ctx.query`.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import zh_api


# =============================================================================
# RepoContext (no network)
# =============================================================================

def make_ctx(owner_repo: str = "acme/widgets",
             repo_id: str = "repo-gid-acme-widgets",
             workspace_id: str = "ws-gid-backend",
             token: str = "fake-token") -> zh_api.RepoContext:
    """Build a RepoContext without any network calls."""
    return zh_api.RepoContext(
        owner_repo=owner_repo,
        repo_id=repo_id,
        workspace_id=workspace_id,
        token=token,
    )


@contextmanager
def patch_ctx_query(ctx: zh_api.RepoContext, responses: list[dict]):
    """Patch `ctx.query` to yield each entry of `responses` in turn.

    Under-supplying responses raises StopIteration — intentional. A
    test that asserts a verb takes N calls but supplies <N responses
    is itself buggy, and we want the failure loud.
    """
    it = iter(responses)

    def _next(*args, **kwargs):  # noqa: ARG001
        return next(it)

    with patch.object(ctx, "query", side_effect=_next):
        yield


# =============================================================================
# `issueByInfo` query response
# =============================================================================

def issue_by_info_response(
    number: int,
    *,
    issue_id: str | None = None,
    title: str | None = None,
    state: str = "OPEN",
    parent: dict | None = None,
    owner: str = "acme",
    repo: str = "widgets",
) -> dict:
    """Single-issue lookup wrapper.

    `parent`, if supplied, should look like
    `{"id": "...", "number": int, "title": "...", "repository": {...}}`.
    `None` (the default) means "no parent" — what `parentIssue` is when
    the issue isn't a sub-issue.
    """
    return {
        "data": {
            "issueByInfo": {
                "id": issue_id or f"issue-gid-{number}",
                "number": number,
                "title": title or f"Issue {number}",
                "state": state,
                "repository": {"ownerName": owner, "name": repo},
                "parentIssue": parent,
            }
        }
    }


def issue_not_found_response() -> dict:
    """`issueByInfo` returns null when the issue doesn't exist."""
    return {"data": {"issueByInfo": None}}


# =============================================================================
# `githubChildIssues` (sub-issue listing) page
# =============================================================================

def child_node(number: int,
               *,
               node_id: str | None = None,
               title: str | None = None,
               state: str = "OPEN",
               assignees: list[str] | None = None,
               pipeline: str | None = None,
               owner: str = "acme",
               repo: str = "widgets") -> dict:
    """One child node inside `githubChildIssues.nodes[]`."""
    return {
        "id": node_id or f"issue-gid-{number}",
        "number": number,
        "title": title or f"Issue {number}",
        "state": state,
        "assignees": {
            "nodes": [{"login": a} for a in (assignees or [])]
        },
        "pipelineIssue": (
            {"pipeline": {"name": pipeline}} if pipeline else None
        ),
        "pipelineIssues": (
            {"nodes": [{"pipeline": {"name": pipeline}}]}
            if pipeline else {"nodes": []}
        ),
        "repository": {"ownerName": owner, "name": repo},
    }


def subissue_list_response(
    *,
    parent_number: int = 42,
    parent_title: str = "Parent",
    parent_state: str = "OPEN",
    total_count: int | None = None,
    nodes: list[dict] | None = None,
    has_next: bool = False,
    end_cursor: str | None = None,
) -> dict:
    """Page of `issueByInfo.githubChildIssues`."""
    nodes = nodes or []
    return {
        "data": {
            "issueByInfo": {
                "number": parent_number,
                "title": parent_title,
                "state": parent_state,
                "githubChildIssues": {
                    "totalCount": (
                        total_count if total_count is not None else len(nodes)
                    ),
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": end_cursor,
                    },
                    "nodes": nodes,
                },
            }
        }
    }


def subissue_parent_not_found() -> dict:
    """Parent lookup returns null in `githubChildIssues`."""
    return {"data": {"issueByInfo": None}}


# =============================================================================
# `addSubIssues` / `removeSubIssues` mutation payloads
# =============================================================================

def add_sub_issues_response(
    *,
    success_count: int = 0,
    failed: list[dict] | None = None,
    github_errors: dict | None = None,
) -> dict:
    """`addSubIssues` mutation response.

    `failed` entries should look like
    `{"number": 42, "repository": {"ownerName": ..., "name": ...}}`.
    """
    return {
        "data": {
            "addSubIssues": {
                "successCount": success_count,
                "failedIssues": failed or [],
                "githubErrors": github_errors or {},
            }
        }
    }


def remove_sub_issues_response(
    *,
    success_count: int = 0,
    failed: list[dict] | None = None,
    github_errors: dict | None = None,
) -> dict:
    """`removeSubIssues` mutation response, same shape as add."""
    return {
        "data": {
            "removeSubIssues": {
                "successCount": success_count,
                "failedIssues": failed or [],
                "githubErrors": github_errors or {},
            }
        }
    }


def reprioritize_sub_issue_response(success: bool = True,
                                    github_errors: dict | None = None) -> dict:
    return {
        "data": {
            "reprioritizeSubIssue": {
                "success": success,
                "githubErrors": github_errors or {},
            }
        }
    }


# =============================================================================
# `workspace.sprints` page
# =============================================================================

def sprint_node(
    sprint_id: str,
    name: str,
    *,
    state: str = "OPEN",
    start: str = "2026-05-01T00:00:00Z",
    end: str = "2026-05-15T00:00:00Z",
    completed: float = 5.0,
    total: float = 13.0,
    closed_count: int = 3,
) -> dict:
    return {
        "id": sprint_id,
        "name": name,
        "state": state,
        "startAt": start,
        "endAt": end,
        "completedPoints": completed,
        "totalPoints": total,
        "closedIssuesCount": closed_count,
    }


def sprints_page(
    nodes: list[dict],
    *,
    has_next: bool = False,
    end_cursor: str | None = None,
    workspace_name: str = "Backend Team",
    active_sprint_id: str | None = "sprint-7",
) -> dict:
    """One page of `workspace.sprints`."""
    return {
        "data": {
            "workspace": {
                "id": "ws-gid-backend",
                "name": workspace_name,
                "activeSprint": (
                    {"id": active_sprint_id, "name": "Sprint 7"}
                    if active_sprint_id else None
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


# =============================================================================
# Sprint detail header / issues
# =============================================================================

def sprint_header_response(
    *,
    sprint_id: str = "sprint-7",
    name: str = "Sprint 7",
    description: str = "Stabilize the auth refactor",
    state: str = "OPEN",
) -> dict:
    return {
        "data": {
            "node": {
                "id": sprint_id,
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


def sprint_header_null() -> dict:
    """Sprint header response with `data.node = null` (deleted/ACL)."""
    return {"data": {"node": None}}


def sprint_issue_wrapper(
    number: int,
    *,
    title: str | None = None,
    state: str = "OPEN",
    estimate: int | None = 3,
    assignees: list[str] | None = None,
    pipeline: str | None = "In Progress",
    owner: str = "acme",
    repo: str = "widgets",
) -> dict:
    """One element of `sprintIssues.nodes[]` (the `{issue: {...}}` wrapper)."""
    return {
        "issue": {
            "number": number,
            "title": title or f"Issue {number}",
            "state": state,
            "htmlUrl": f"https://github.com/{owner}/{repo}/issues/{number}",
            "estimate": ({"value": estimate} if estimate is not None else None),
            "assignees": {"nodes": [{"login": a} for a in (assignees or [])]},
            "repository": {"ownerName": owner, "name": repo},
            "pipelineIssues": {
                "nodes": (
                    [{"pipeline": {"name": pipeline}}] if pipeline else []
                )
            },
        }
    }


def sprint_issues_page(
    nodes: list[dict],
    *,
    has_next: bool = False,
    end_cursor: str | None = None,
) -> dict:
    """One page of `node.sprintIssues` (the paginated walker shape)."""
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


def sprint_issues_null_node() -> dict:
    """Walker page response when `data.node` is null."""
    return {"data": {"node": None}}


# =============================================================================
# Sprint membership mutations
# =============================================================================

def add_issues_to_sprints_response(
    *,
    sprint_id: str = "sprint-7",
    linked: list[tuple[int, str, str]] | None = None,
) -> dict:
    """`addIssuesToSprints` mutation response.

    `linked` entries: `(issue_number, owner, repo)` tuples describing
    each SprintIssue link that was created. Inputs absent from this
    list are inferred-failed by the production code.
    """
    linked = linked or []
    return {
        "data": {
            "addIssuesToSprints": {
                "sprintIssues": [
                    {
                        "id": f"link-{n}-{sprint_id}",
                        "issue": {
                            "number": n,
                            "repository": {"ownerName": o, "name": r},
                        },
                        "sprint": {"id": sprint_id},
                    }
                    for (n, o, r) in linked
                ]
            }
        }
    }


def remove_issues_from_sprints_response(
    *,
    sprint_id: str = "sprint-7",
    still_attached: list[tuple[int, str, str]] | None = None,
    omit_target: bool = False,
    empty_sprints: bool = False,
) -> dict:
    """`removeIssuesFromSprints` mutation response.

    `still_attached` entries: `(issue_number, owner, repo)` tuples
    that remain in the sprint after the mutation. Anything in
    `issue_numbers` that doesn't appear here is treated as removed.

    `omit_target=True` simulates the API anomaly where the target
    sprint is missing from the response (a different sprint's state
    is returned instead). `empty_sprints=True` simulates an empty
    `sprints` array.
    """
    if empty_sprints:
        return {"data": {"removeIssuesFromSprints": {"sprints": []}}}
    if omit_target:
        return {
            "data": {
                "removeIssuesFromSprints": {
                    "sprints": [
                        {
                            "id": "sprint-OTHER",
                            "sprintIssues": {"nodes": []},
                        }
                    ]
                }
            }
        }
    still_attached = still_attached or []
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
                                            "ownerName": o, "name": r,
                                        },
                                    }
                                }
                                for (n, o, r) in still_attached
                            ]
                        },
                    }
                ]
            }
        }
    }
