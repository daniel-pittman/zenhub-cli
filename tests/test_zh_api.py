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
