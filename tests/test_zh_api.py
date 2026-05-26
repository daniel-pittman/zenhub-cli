"""Tests for the zh_api auth + config layer."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import zh_api


def test_load_config_parses_simple_kv(tmp_path: Path):
    path = tmp_path / "config"
    path.write_text(
        "# Comment line\n"
        "\n"
        "ZH_TOKEN=abc123\n"
        "ZH_REST_TOKEN=def456\n"
        "ZH_WORKSPACE=My Team\n"
    )
    cfg = zh_api.load_config(path)
    assert cfg["ZH_TOKEN"] == "abc123"
    assert cfg["ZH_REST_TOKEN"] == "def456"
    assert cfg["ZH_WORKSPACE"] == "My Team"


def test_load_config_handles_quotes_and_export(tmp_path: Path):
    """Quoted values + export prefix both supported."""
    path = tmp_path / "config"
    path.write_text(
        'export ZH_TOKEN="abc123"\n'
        "ZH_WORKSPACE='Backend Team'\n"
    )
    cfg = zh_api.load_config(path)
    assert cfg["ZH_TOKEN"] == "abc123"
    assert cfg["ZH_WORKSPACE"] == "Backend Team"


def test_load_config_missing_file_returns_empty(tmp_path: Path):
    cfg = zh_api.load_config(tmp_path / "no-such-file")
    assert cfg == {}


def test_resolve_token_env_wins_over_config(monkeypatch):
    monkeypatch.setenv("ZH_TOKEN", "from-env")
    cfg = {"ZH_TOKEN": "from-config"}
    assert zh_api.resolve_token(cfg) == "from-env"


def test_resolve_token_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("ZH_TOKEN", raising=False)
    cfg = {"ZH_TOKEN": "from-config"}
    assert zh_api.resolve_token(cfg) == "from-config"


def test_resolve_token_empty_raises(monkeypatch):
    monkeypatch.delenv("ZH_TOKEN", raising=False)
    with pytest.raises(zh_api.ZhApiError):
        zh_api.resolve_token({"ZH_TOKEN": ""})


def test_repos_match_handles_missing_fields():
    """Defense in depth on the case-insensitive comparison."""
    assert not zh_api.repos_match({}, "acme/widgets")
    assert not zh_api.repos_match(
        {"ownerName": "acme"}, "acme/widgets"  # missing name
    )
    assert not zh_api.repos_match(
        {"name": "widgets"}, "acme/widgets"  # missing ownerName
    )


def test_owner_repo_url_regex():
    """Cover the common GitHub URL forms `zh` accepts."""
    cases = [
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("git@github.com:acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("http://github.com/acme/widgets/", "acme/widgets"),
    ]
    for url, expected in cases:
        m = zh_api._GH_URL_RE.search(url)
        assert m, f"failed to match {url!r}"
        assert f"{m.group('owner')}/{m.group('repo')}" == expected


def test_owner_repo_url_regex_with_dots_in_repo_name():
    """Review finding #1: repo names with dots used to be rejected.

    GitHub allows dots in repo names (`docs.github.io`, `my.tool`,
    `internal.docs`). The old `[^/.]+?` for the repo group silently
    failed to match those. Fix changes it to `[^/]+?` with the
    optional `\\.git` tail still claimed correctly.
    """
    cases = [
        ("git@github.com:acme/docs.github.io.git", "acme/docs.github.io"),
        ("https://github.com/acme/docs.github.io", "acme/docs.github.io"),
        ("https://github.com/acme/internal.docs.git", "acme/internal.docs"),
        ("git@github.com:acme/my.tool", "acme/my.tool"),
        # Owner with dots also works (always did, but worth pinning)
        ("https://github.com/owner.with.dots/repo", "owner.with.dots/repo"),
    ]
    for url, expected in cases:
        m = zh_api._GH_URL_RE.search(url)
        assert m, f"failed to match {url!r}"
        assert f"{m.group('owner')}/{m.group('repo')}" == expected, (
            f"got {m.group('owner')}/{m.group('repo')} for {url!r}"
        )


def test_garbage_prefix_rejected():
    """Round-5 finding #6: without `^` anchor, `re.search` would
    accept a garbage prefix by matching the URL substring.
    Canonical contract: input must START with one of the two
    accepted scheme forms.
    """
    garbage_inputs = [
        "prefix-junk-git@github.com:owner/repo",
        "noise https://github.com/owner/repo",
        # Embedded newline + valid URL on second line (re.search would
        # accept this without the anchor + MULTILINE caveats)
        "\nhttps://github.com/owner/repo",
        # Leading whitespace
        " git@github.com:owner/repo",
    ]
    for url in garbage_inputs:
        m = zh_api._GH_URL_RE.search(url)
        assert m is None, (
            f"garbage-prefixed URL {url!r} should NOT match; got {m!r}"
        )


# =============================================================================
# list_workspaces pagination (review finding #6)
# =============================================================================

def _ws_page(nodes: list[dict], *, has_next: bool = False,
             end_cursor: str | None = None) -> dict:
    """Workspaces-connection page wrapper used by the tests below."""
    return {
        "data": {
            "repositoriesByGhId": [{
                "id": "repo-gid-123",
                "workspacesConnection": {
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": end_cursor,
                    },
                    "nodes": nodes,
                },
            }]
        }
    }


def test_list_workspaces_walks_pagination(monkeypatch):
    """A repo connected to >50 workspaces — name lookup must walk pages.

    Review finding #6: with the workspace cap at 50 nodes, ZH_WORKSPACE
    lookup against an enterprise repo could silently miss workspaces
    beyond position 50.
    """
    monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)

    page_one = [{"id": f"ws-{i}", "name": f"Workspace {i}"} for i in range(50)]
    page_two = [{"id": "ws-50", "name": "Older Workspace"}]
    responses = iter([
        _ws_page(page_one, has_next=True, end_cursor="cursor-2"),
        _ws_page(page_two, has_next=False),
    ])
    monkeypatch.setattr(
        zh_api, "graphql_request",
        lambda *a, **kw: next(responses),
    )
    nodes = zh_api.list_workspaces("acme/widgets", token="t", gh_token="t")
    names = {n["name"] for n in nodes}
    assert "Older Workspace" in names
    assert len(nodes) == 51


def test_get_workspace_id_resolves_name_on_page_two(monkeypatch):
    """End-to-end: name lookup hits a workspace on page 2."""
    monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)
    responses = iter([
        _ws_page(
            [{"id": f"ws-{i}", "name": f"Front {i}"} for i in range(50)],
            has_next=True, end_cursor="cursor-2",
        ),
        _ws_page(
            [{"id": "ws-deep", "name": "Deep Workspace"}],
            has_next=False,
        ),
    ])
    monkeypatch.setattr(
        zh_api, "graphql_request",
        lambda *a, **kw: next(responses),
    )
    ws_id = zh_api.get_workspace_id(
        "acme/widgets",
        workspace_name="deep workspace",
        token="t", gh_token="t",
    )
    assert ws_id == "ws-deep"


def test_list_workspaces_stuck_cursor_bails(monkeypatch):
    """Server reports hasNextPage=true but cursor never advances."""
    monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)
    stuck = _ws_page(
        [{"id": "ws-a", "name": "A"}],
        has_next=True,
        end_cursor=None,  # explicitly missing
    )
    monkeypatch.setattr(
        zh_api, "graphql_request",
        lambda *a, **kw: stuck,
    )
    # Should return whatever was collected before bailing, not spin.
    nodes = zh_api.list_workspaces("acme/widgets", token="t", gh_token="t")
    assert {n["id"] for n in nodes} == {"ws-a"}


# =============================================================================
# Env-var contract: resolve_context honors the same vars bash exports
# =============================================================================

def _patch_context_deps(monkeypatch):
    """Stub out the network parts of resolve_context."""
    monkeypatch.setattr(zh_api, "resolve_token", lambda config=None: "tok")
    monkeypatch.setattr(zh_api, "get_zenhub_repo_id", lambda *a, **kw: "repo-gid")
    monkeypatch.setattr(
        zh_api, "get_workspace_id",
        lambda owner_repo, **kw: f"ws-for-{kw.get('workspace_name') or 'default'}",
    )
    monkeypatch.setattr(zh_api, "load_config", lambda *a, **kw: {})


def test_resolve_context_reads_zh_workspace_name(monkeypatch):
    """ZH_WORKSPACE_NAME (set by bash `-w` flag) is honored.

    The bash `main()` arg parser exports ZH_WORKSPACE_NAME when it sees
    `-w "Backend Team"`. Python `resolve_context` must read the same
    var, otherwise the flag silently drops on the Python side.
    """
    _patch_context_deps(monkeypatch)
    monkeypatch.setenv("ZH_WORKSPACE_NAME", "Backend Team")
    # Set ZH_WORKSPACE to a different value to make sure _NAME wins
    monkeypatch.setenv("ZH_WORKSPACE", "Should Be Ignored")
    ctx = zh_api.resolve_context(owner_repo="acme/widgets")
    # get_workspace_id stub bakes the name into the returned id
    assert ctx.workspace_id == "ws-for-Backend Team"


def test_resolve_context_falls_back_to_zh_workspace(monkeypatch):
    """If only ZH_WORKSPACE is set (config-style), it still works."""
    _patch_context_deps(monkeypatch)
    monkeypatch.delenv("ZH_WORKSPACE_NAME", raising=False)
    monkeypatch.setenv("ZH_WORKSPACE", "From Config")
    ctx = zh_api.resolve_context(owner_repo="acme/widgets")
    assert ctx.workspace_id == "ws-for-From Config"


def test_resolve_context_reads_zh_repo_override(monkeypatch):
    """ZH_REPO_OVERRIDE (set by bash `-r` flag) wins over ZH_REPO."""
    _patch_context_deps(monkeypatch)
    monkeypatch.setenv("ZH_REPO_OVERRIDE", "flag/repo")
    monkeypatch.setenv("ZH_REPO", "env/repo")
    ctx = zh_api.resolve_context()
    assert ctx.owner_repo == "flag/repo"


def test_resolve_context_falls_back_to_zh_repo(monkeypatch):
    """ZH_REPO (env or config) wins over the git-remote default."""
    _patch_context_deps(monkeypatch)
    monkeypatch.delenv("ZH_REPO_OVERRIDE", raising=False)
    monkeypatch.setenv("ZH_REPO", "env/repo")
    # Don't stub get_owner_repo_from_git — if our env handling is
    # broken and we fall through, the call will raise (no git remote
    # in pytest cwd).
    ctx = zh_api.resolve_context()
    assert ctx.owner_repo == "env/repo"


def test_resolve_context_explicit_arg_wins(monkeypatch):
    """Explicit owner_repo arg trumps every env var."""
    _patch_context_deps(monkeypatch)
    monkeypatch.setenv("ZH_REPO_OVERRIDE", "ignored/override")
    monkeypatch.setenv("ZH_REPO", "ignored/env")
    ctx = zh_api.resolve_context(owner_repo="arg/repo")
    assert ctx.owner_repo == "arg/repo"


def test_resolve_context_explicit_workspace_arg_wins(monkeypatch):
    """Explicit workspace_name arg trumps every env var."""
    _patch_context_deps(monkeypatch)
    monkeypatch.setenv("ZH_WORKSPACE_NAME", "ignored-flag-name")
    monkeypatch.setenv("ZH_WORKSPACE", "ignored-config-name")
    ctx = zh_api.resolve_context(
        owner_repo="acme/widgets", workspace_name="From Arg"
    )
    assert ctx.workspace_id == "ws-for-From Arg"
