"""Tests for mcp_server.py helper functions that have no network side
effects: the venv bootstrap predicates and the `zh mine` output parser.

These run with ZH_MCP_SKIP_BOOTSTRAP=1 (set by conftest.py) so importing
mcp_server doesn't trigger the real venv build.
"""

from __future__ import annotations

import os
from pathlib import Path

import mcp_server


# -----------------------------------------------------------------------------
# _deps_hash
# -----------------------------------------------------------------------------


def test_deps_hash_deterministic():
    assert mcp_server._deps_hash() == mcp_server._deps_hash()


def test_deps_hash_changes_when_deps_change(monkeypatch):
    base = mcp_server._deps_hash()
    monkeypatch.setattr(mcp_server, "_VENV_DEPS", ("mcp", "numpy"))
    assert mcp_server._deps_hash() != base


# -----------------------------------------------------------------------------
# _default_venv_dir — env-driven location resolution
# -----------------------------------------------------------------------------


def test_default_venv_dir_uses_zh_mcp_venv_when_set(monkeypatch, tmp_path):
    custom = tmp_path / "custom-venv"
    monkeypatch.setenv("ZH_MCP_VENV", str(custom))
    assert mcp_server._default_venv_dir() == custom


def test_default_venv_dir_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert mcp_server._default_venv_dir() == tmp_path / "zh" / "venv"


def test_default_venv_dir_fallback(monkeypatch):
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path(os.path.expanduser("~/.local/share")) / "zh" / "venv"
    assert mcp_server._default_venv_dir() == expected


# -----------------------------------------------------------------------------
# _venv_is_valid — failure modes that have happened in practice
# -----------------------------------------------------------------------------


def _make_fake_venv(
    base: Path, *, with_python: bool, with_cfg: bool,
    marker_value: str | None,
) -> Path:
    """Scaffold a synthetic venv-shaped tree without actually running
    `python -m venv`. Useful for exercising the file-existence checks."""
    (base / "bin").mkdir(parents=True, exist_ok=True)
    if with_python:
        py = base / "bin" / "python3"
        py.write_text("#!/bin/sh\nexit 0\n")
        py.chmod(0o755)
    if with_cfg:
        (base / "pyvenv.cfg").write_text("home = /fake\nversion = 3.11.0\n")
    if marker_value is not None:
        (base / mcp_server._VENV_MARKER).write_text(marker_value)
    return base


def test_venv_invalid_when_python_missing(tmp_path):
    _make_fake_venv(
        tmp_path, with_python=False, with_cfg=True, marker_value="abc",
    )
    assert not mcp_server._venv_is_valid(tmp_path, "abc")


def test_venv_invalid_when_pyvenv_cfg_missing(tmp_path):
    # This is the exact failure mode that hit the v1.6.0 release — the
    # binary survived but pyvenv.cfg was gone, leaving the venv's
    # site-packages unreachable.
    _make_fake_venv(
        tmp_path, with_python=True, with_cfg=False, marker_value="abc",
    )
    assert not mcp_server._venv_is_valid(tmp_path, "abc")


def test_venv_invalid_when_marker_missing(tmp_path):
    _make_fake_venv(
        tmp_path, with_python=True, with_cfg=True, marker_value=None,
    )
    assert not mcp_server._venv_is_valid(tmp_path, "abc")


def test_venv_invalid_when_marker_mismatch(tmp_path):
    # Simulates _VENV_DEPS changing between launches.
    _make_fake_venv(
        tmp_path, with_python=True, with_cfg=True, marker_value="stale-hash",
    )
    assert not mcp_server._venv_is_valid(tmp_path, "current-hash")


# Note: the success path (everything present + `import mcp` works) is
# covered by the post-build sanity check inside _build_venv at runtime
# and isn't unit-testable without standing up a real venv (slow + fragile
# in CI). The "all four file checks pass but mcp import fails" path is
# implicitly exercised whenever pre-existing /tmp/zhenv-shaped venvs land
# on a developer's machine and get rejected.


# -----------------------------------------------------------------------------
# _parse_mine_listing — fixes the v1.5/v1.6 drift where `zh mine`'s
# 3-field shape didn't match the pipeline-listing parser's 4-field regex.
# -----------------------------------------------------------------------------


_MINE_SAMPLE = """\
Info: Finding issues assigned to acme-user...

Issues assigned to acme-user (3):

  #645 │ acme/widget-app │ Product Backlog
    Add loading state to account screen
    → https://app.zenhub.com/workspaces/W/issues/gh/acme/widget-app/645

  #108 │ acme/widget-server │ In Progress
    fix: badge tier filter uses startsWith
    → https://app.zenhub.com/workspaces/W/issues/gh/acme/widget-server/108

  #646 │ acme/widget-app │ In Progress
    fix: admin wizard bottom-action Row overflows at phone widths
    → https://app.zenhub.com/workspaces/W/issues/gh/acme/widget-app/646
"""


def test_parse_mine_listing_extracts_all_three_fields():
    issues = mcp_server._parse_mine_listing(_MINE_SAMPLE)
    assert len(issues) == 3
    assert issues[0] == {
        "number": 645,
        "repo": "acme/widget-app",
        "pipeline": "Product Backlog",
        "title": "Add loading state to account screen",
    }
    assert issues[1]["number"] == 108
    assert issues[1]["pipeline"] == "In Progress"
    assert issues[2]["title"].startswith("fix: admin wizard")


def test_parse_mine_listing_empty_input():
    assert mcp_server._parse_mine_listing("") == []


def test_parse_mine_listing_ignores_header_lines():
    # No `#NNN │ ...` lines means no entries even though the header
    # text is present.
    header_only = (
        "Info: Finding issues assigned to acme-user...\n"
        "\n"
        "Issues assigned to acme-user (0):\n"
    )
    assert mcp_server._parse_mine_listing(header_only) == []
