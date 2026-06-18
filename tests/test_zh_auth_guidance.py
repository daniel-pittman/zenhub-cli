"""Regression tests for the lapsed-ZenHub<->GitHub-auth guidance.

When ZenHub's GitHub authorization expires (independently of ZH_TOKEN), the
token still authenticates and workspaces stay readable, but ZenHub can no
longer see GitHub repo objects: `repositoriesByGhId` comes back EMPTY (no
top-level GraphQL error) and `createIssue` returns a NO_ACCESS error. The
pre-existing messages ("Repository not found in ZenHub" / "No workspace found")
gave no hint that the fix is re-authorizing on the website, which sent a real
debugging session down the wrong path (token / repo-attachment).

These pin that:
  - the empty-result resolution paths (get_repo_id / get_workspace_id) now emit
    the actionable two-cause guidance (re-auth first, then repo attachment), and
  - the NO_ACCESS mutation path (zh_graphql) appends the re-auth hint, while a
    GENERIC GraphQL error does NOT (so the hint stays specific).
"""

from __future__ import annotations

from _bash_runner import run_zh_with_stubs

_REAUTH = "app.zenhub.com"
_AUTH_PHRASE = "GitHub authorization"


def test_get_repo_id_empty_result_gives_auth_guidance() -> None:
    """ZenHub returns the repo as empty (lapsed auth or unattached): the error
    must lead with the re-authorize-on-the-website guidance, not the bare
    'Repository not found'."""
    stubs = r"""
        resolve_gh_repo_id() { printf '12345'; }
        zh_graphql() { printf '%s' '{"data":{"repositoriesByGhId":[]}}'; }
    """
    r = run_zh_with_stubs(stubs, "get_repo_id acme/widgets")
    assert r.returncode == 1
    assert _REAUTH in r.stderr, f"missing re-auth URL; got {r.stderr!r}"
    assert _AUTH_PHRASE in r.stderr
    assert "Manage Repositories" in r.stderr, "should also name the attach-repo cause"
    assert "acme/widgets" in r.stderr


def test_get_workspace_id_empty_result_gives_auth_guidance() -> None:
    """Same root cause on the workspace-resolution path (node_count == 0)."""
    stubs = r"""
        resolve_gh_repo_id() { printf '12345'; }
        zh_workspaces_fetch_all() { printf '%s' '[]'; }
    """
    r = run_zh_with_stubs(stubs, "get_workspace_id acme/widgets")
    assert r.returncode == 1
    assert _REAUTH in r.stderr, f"missing re-auth URL; got {r.stderr!r}"
    assert _AUTH_PHRASE in r.stderr


def test_zh_graphql_no_access_error_appends_reauth_hint() -> None:
    """A NO_ACCESS GraphQL error (the createIssue face of lapsed auth) must get
    the re-auth hint appended to the surfaced error."""
    stubs = r"""
        curl() { printf '%s' '{"errors":[{"type":"NO_ACCESS","message":"No access to repository"}]}'; }
    """
    r = run_zh_with_stubs(stubs, 'zh_graphql "query{viewer{id}}" "{}"')
    assert r.returncode == 1
    assert "No access to repository" in r.stderr
    assert _REAUTH in r.stderr, f"NO_ACCESS should append the re-auth hint; got {r.stderr!r}"


def test_zh_graphql_no_access_via_extensions_code() -> None:
    """The hint also fires when NO_ACCESS is carried in extensions.code rather
    than .type (GraphQL servers vary)."""
    stubs = r"""
        curl() { printf '%s' '{"errors":[{"message":"denied","extensions":{"code":"NO_ACCESS"}}]}'; }
    """
    r = run_zh_with_stubs(stubs, 'zh_graphql "query{viewer{id}}" "{}"')
    assert r.returncode == 1
    assert _REAUTH in r.stderr


def test_zh_graphql_generic_error_has_no_reauth_hint() -> None:
    """A non-access GraphQL error (e.g. a bad field) must NOT get the re-auth
    hint — the guidance has to stay specific to access failures, or it becomes
    noise on every unrelated error."""
    stubs = r"""
        curl() { printf '%s' '{"errors":[{"message":"Cannot query field foo on type Query"}]}'; }
    """
    r = run_zh_with_stubs(stubs, 'zh_graphql "query{foo}" "{}"')
    assert r.returncode == 1
    assert "Cannot query field foo" in r.stderr
    assert _REAUTH not in r.stderr, f"generic error must not get the re-auth hint; got {r.stderr!r}"
