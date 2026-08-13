"""Tests for mcp_server.py helper functions that have no network side
effects: the venv bootstrap predicates and the `zh mine` output parser.

These run with ZH_MCP_SKIP_BOOTSTRAP=1 (set by conftest.py) so importing
mcp_server doesn't trigger the real venv build.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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


# `_default_venv_dir()` calls `.resolve()` on both branches — assertions
# compare resolved paths so macOS's /var → /private/var firmlink and any
# Linux symlink in $TMPDIR / $HOME don't break the test cross-platform.


def test_default_venv_dir_uses_zh_mcp_venv_when_set(monkeypatch, tmp_path):
    custom = tmp_path / "custom-venv"
    monkeypatch.setenv("ZH_MCP_VENV", str(custom))
    venv_dir, user_supplied = mcp_server._default_venv_dir()
    assert venv_dir == custom.resolve()
    assert user_supplied is True


def test_default_venv_dir_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    venv_dir, user_supplied = mcp_server._default_venv_dir()
    assert venv_dir == (tmp_path / "zh" / "venv").resolve()
    assert user_supplied is False


def test_default_venv_dir_fallback(monkeypatch):
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = (
        Path(os.path.expanduser("~/.local/share")) / "zh" / "venv"
    ).resolve()
    venv_dir, user_supplied = mcp_server._default_venv_dir()
    assert venv_dir == expected
    assert user_supplied is False


def test_default_venv_dir_resolves_relative_xdg(monkeypatch, tmp_path):
    # Relative XDG_DATA_HOME must be pinned to an absolute path at
    # startup, otherwise launches from different cwds would compute
    # different venv locations and rebuild-loop.
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", "local-data")
    venv_dir, user_supplied = mcp_server._default_venv_dir()
    assert venv_dir.is_absolute()
    assert venv_dir == (tmp_path / "local-data" / "zh" / "venv").resolve()
    assert user_supplied is False


# -----------------------------------------------------------------------------
# _venv_is_valid — failure modes that have happened in practice
# -----------------------------------------------------------------------------


def _make_fake_venv(
    base: Path, *, with_python: bool, with_cfg: bool,
    marker_value: str | None, python_exits: int = 1,
) -> Path:
    """Scaffold a synthetic venv-shaped tree without actually running
    `python -m venv`. Useful for exercising the file-existence checks.

    `python_exits` controls the fake `bin/python3` stub's exit code.
    Default is 1 (probe FAILS) — so invalid-path tests that never reach
    the probe stay correct, AND any future happy-path test that wants
    to exercise probe success has to opt in by passing `python_exits=0`
    explicitly. Prevents a footgun where a future test silently false-
    positives because POSIX `sh` runs `exit 0` and ignores `-c <probe>`.
    """
    (base / "bin").mkdir(parents=True, exist_ok=True)
    if with_python:
        py = base / "bin" / "python3"
        py.write_text(f"#!/bin/sh\nexit {python_exits}\n")
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


def test_venv_invalid_when_marker_unreadable(tmp_path):
    # Simulates a corrupt / root-owned / non-UTF-8 marker file. The
    # guard must return False (triggering a rebuild) rather than letting
    # the exception escape and crash the MCP bootstrap.
    _make_fake_venv(
        tmp_path, with_python=True, with_cfg=True, marker_value=None,
    )
    # Write non-UTF-8 bytes directly — read_text() raises UnicodeDecodeError.
    (tmp_path / mcp_server._VENV_MARKER).write_bytes(b"\xff\xfe\xfd")
    assert not mcp_server._venv_is_valid(tmp_path, "any-hash")


# Note: the full success path (file checks pass + `import mcp` works) is
# covered by the post-build sanity check inside _build_venv at runtime,
# not in this unit-test file. Standing up a real `python -m venv` here
# would slow the suite by ~1s for one assertion. Tracked as a v1.6.1
# follow-up if the file-shape tests above prove insufficient.


# -----------------------------------------------------------------------------
# _looks_like_zh_venv — guards the destructive `shutil.rmtree` path so a
# typo in ZH_MCP_VENV (or XDG_DATA_HOME) can't wipe arbitrary user data.
# -----------------------------------------------------------------------------


def test_looks_like_zh_venv_accepts_missing(tmp_path):
    # Nothing there yet — nothing to lose, safe to "rebuild" (i.e. build
    # from scratch). The bootstrap relies on this returning True so the
    # first-ever launch can proceed.
    target = tmp_path / "does-not-exist"
    assert mcp_server._looks_like_zh_venv(target)


def test_looks_like_zh_venv_accepts_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    assert mcp_server._looks_like_zh_venv(tmp_path / "empty")


def test_looks_like_zh_venv_accepts_dir_with_our_marker(tmp_path):
    (tmp_path / mcp_server._VENV_MARKER).write_text("any-hash")
    (tmp_path / "decoy-junk").write_text("anything")
    assert mcp_server._looks_like_zh_venv(tmp_path)


def test_looks_like_zh_venv_refuses_foreign_venv(tmp_path):
    # The footgun the round-2 review caught: a user sets
    # ZH_MCP_VENV=~/projects/myrepo/.venv (their own project venv). It
    # has pyvenv.cfg but NO _VENV_MARKER. The guard must refuse rather
    # than rmtree it — `pyvenv.cfg` alone is not sufficient evidence
    # that this venv belongs to us.
    (tmp_path / "pyvenv.cfg").write_text("home = /fake\nversion = 3.11.0\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").touch(mode=0o755)
    # site-packages with the user's editable install — losing this is
    # the actual damage scenario.
    (tmp_path / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    (tmp_path / "lib" / "python3.11" / "site-packages" / "myrepo.pth").write_text(
        "/home/user/projects/myrepo/src\n"
    )
    assert not mcp_server._looks_like_zh_venv(tmp_path)


def test_looks_like_zh_venv_refuses_non_venv_directory(tmp_path):
    # ZH_MCP_VENV pointed at a project dir by accident (e.g. omitted
    # `/.venv` suffix). Has contents but no pyvenv.cfg and no marker —
    # must refuse.
    (tmp_path / "important-document.txt").write_text("don't delete me")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")
    assert not mcp_server._looks_like_zh_venv(tmp_path)


def test_looks_like_zh_venv_refuses_regular_file(tmp_path):
    # ZH_MCP_VENV pointed at a regular file (e.g. `~/.bashrc` typo).
    # `iterdir()` would raise NotADirectoryError without the is_dir guard.
    target = tmp_path / "some-file.txt"
    target.write_text("not a venv")
    assert not mcp_server._looks_like_zh_venv(target)


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


def test_parse_mine_listing_rejects_four_field_shape():
    # If `zh mine` ever grows a 4th column (or the MCP starts invoking
    # `--no-urls` mode, which pads the issue number and adds a field),
    # the tightened pipeline-capture (`[^│]+?` instead of `.+?`) refuses
    # to match the line rather than silently swallowing the extra column
    # into `pipeline`. The line is skipped — explicit miss instead of
    # silent data corruption.
    four_field = (
        "  #645 │ acme/widget-app │ In Progress │ extra-field\n"
        "    a title line\n"
    )
    assert mcp_server._parse_mine_listing(four_field) == []


def test_parse_mine_listing_skips_unindented_banner():
    # If `zh mine` ever emits an interstitial unindented line between
    # the header and the title (group banner, gh warning, etc.), it must
    # NOT be silently captured as the title. The walker continues past
    # unindented lines and finds the real indented title below.
    sample = (
        "  #645 │ acme/widget-app │ Product Backlog\n"
        "WARNING: gh emitted a banner here without indentation\n"
        "    Add loading state to account screen\n"
        "    → https://app.zenhub.com/anywhere/645\n"
    )
    issues = mcp_server._parse_mine_listing(sample)
    assert len(issues) == 1
    assert issues[0]["title"] == "Add loading state to account screen"


def test_parse_mine_listing_title_starting_with_hash():
    # Real bug class: a legitimate issue title starting with `#` (e.g.
    # `#perf-2026Q2: foo` or markdown-style `# Goals`) used to trip the
    # title-bail-out loop, which only checked `startswith("#")` and
    # exited immediately, leaving title="". Tightened bail-out matches
    # the full issue-header shape (`#NNN │ ...`) so titles can start
    # with `#` and still be captured.
    sample = (
        "  #645 │ acme/widget-app │ Product Backlog\n"
        "    #perf-2026Q2: rework batching across hot paths\n"
        "    → https://app.zenhub.com/anywhere/645\n"
    )
    issues = mcp_server._parse_mine_listing(sample)
    assert len(issues) == 1
    assert issues[0]["title"] == "#perf-2026Q2: rework batching across hot paths"


# -----------------------------------------------------------------------------
# _parse_pipeline_listing — the 4-field `#N │ repo │ N pts │ assignee`
# parser shipped in v1.5.0. The PR tightened its title-walker (require
# indentation, match full `#NNN │` header shape); these tests pin that
# behavior so a future regression surfaces in CI instead of as silent
# data corruption in the pipeline MCP tool.
# -----------------------------------------------------------------------------


_PIPELINE_SAMPLE = """\
Info: Getting issues in 'In Progress'...

Pipeline: In Progress (3 issues)

  #108 │ acme/widget-server │ — pts │ acme-user
    fix: badge tier filter uses startsWith
    → https://app.zenhub.com/workspaces/W/issues/gh/acme/widget-server/108

  #646 │ acme/widget-app │ 3 pts │ acme-user
    fix: admin wizard bottom-action Row overflows
    → https://app.zenhub.com/workspaces/W/issues/gh/acme/widget-app/646

  #613 │ acme/widget-app │ — pts │ unassigned
    Implement Leaderboard & Rewards screens
    → https://app.zenhub.com/workspaces/W/issues/gh/acme/widget-app/613
"""


def test_parse_pipeline_listing_extracts_all_four_fields():
    issues = mcp_server._parse_pipeline_listing(_PIPELINE_SAMPLE)
    assert len(issues) == 3
    assert issues[0] == {
        "number": 108,
        "repo": "acme/widget-server",
        "estimate": None,
        "assignee": "acme-user",
        "title": "fix: badge tier filter uses startsWith",
    }
    assert issues[1]["estimate"] == "3"
    assert issues[2]["assignee"] is None  # "unassigned" normalized


def test_parse_pipeline_listing_skips_unindented_banner():
    sample = (
        "  #108 │ acme/widget-server │ — pts │ acme-user\n"
        "WARNING: gh emitted a banner here without indentation\n"
        "    fix: badge tier filter uses startsWith\n"
        "    → https://app.zenhub.com/anywhere/108\n"
    )
    issues = mcp_server._parse_pipeline_listing(sample)
    assert len(issues) == 1
    assert issues[0]["title"] == "fix: badge tier filter uses startsWith"


def test_parse_pipeline_listing_title_starting_with_hash():
    sample = (
        "  #108 │ acme/widget-server │ — pts │ acme-user\n"
        "    #perf-2026Q2: rework hot-path batching\n"
        "    → https://app.zenhub.com/anywhere/108\n"
    )
    issues = mcp_server._parse_pipeline_listing(sample)
    assert len(issues) == 1
    assert issues[0]["title"] == "#perf-2026Q2: rework hot-path batching"


# -----------------------------------------------------------------------------
# _ISSUE_HEADER_RE bounded-indent check — round-4 #2.
# zh emits headers at 2-space indent, titles at 4-space indent. The
# regex must NOT match a 4-space-indented title that legitimately
# starts with `#NNN │ ...` (cross-reference convention some teams use).
# -----------------------------------------------------------------------------


def test_parse_mine_listing_title_with_hash_pipe_pattern():
    # A title like `#1234 │ blocker note` at 4-space indent must NOT
    # trigger the "next issue header" bail-out. The header regex is
    # bounded to 0-3 leading spaces; titles at 4 spaces fall through.
    sample = (
        "  #645 │ acme/widget-app │ Product Backlog\n"
        "    #1234 │ blocker note for OAuth retry path\n"
        "    → https://app.zenhub.com/anywhere/645\n"
    )
    issues = mcp_server._parse_mine_listing(sample)
    assert len(issues) == 1
    assert issues[0]["title"] == "#1234 │ blocker note for OAuth retry path"


def test_parse_pipeline_listing_title_with_hash_pipe_pattern():
    sample = (
        "  #108 │ acme/widget-server │ — pts │ acme-user\n"
        "    #1234 │ blocker note for OAuth retry path\n"
        "    → https://app.zenhub.com/anywhere/108\n"
    )
    issues = mcp_server._parse_pipeline_listing(sample)
    assert len(issues) == 1
    assert issues[0]["title"] == "#1234 │ blocker note for OAuth retry path"


# -----------------------------------------------------------------------------
# _parse_create_json (v1.9.0 G2). `zh create --json` emits a single JSON
# object on stdout (human chatter goes to stderr). The MCP create_issue /
# planning *_create tools parse the new number from this object rather than
# scraping a colorized success line. The v1.5-era `_parse_new_issue_number` /
# `_parse_new_epic_number` helpers were retired with the migration and the
# tests pinning them deleted alongside.
# -----------------------------------------------------------------------------


def test_parse_create_json_pure_object():
    stdout = (
        '{"number": 42, "url": "https://github.com/o/r/issues/42", '
        '"title": "Auth service", "type": "Epic", "pipeline": null, '
        '"estimate": null, "parent": null}\n'
    )
    obj = mcp_server._parse_create_json(stdout)
    assert obj is not None
    assert obj["number"] == 42
    assert obj["type"] == "Epic"
    assert obj["url"].endswith("/42")


def test_parse_create_json_with_leading_noise():
    # Defensive: a wrapper prepends a stray line. We still find the object.
    stdout = 'stray prefix line\n{"number": 7, "type": "Feature"}\n'
    obj = mcp_server._parse_create_json(stdout)
    assert obj is not None
    assert obj["number"] == 7
    assert obj["type"] == "Feature"


def test_parse_create_json_returns_none_on_no_json():
    assert mcp_server._parse_create_json("Error: type not found\n") is None
    assert mcp_server._parse_create_json("") is None


def test_parse_create_json_ignores_non_object_json():
    # A bare JSON array / scalar is not a create object.
    assert mcp_server._parse_create_json("[1, 2, 3]") is None
    assert mcp_server._parse_create_json("42") is None


# -----------------------------------------------------------------------------
# _default_venv_dir empty ZH_MCP_VENV — round-4 #10.
# An empty / whitespace value must NOT silently fall through to XDG
# without diagnostic. The fall-through is preserved (so CI configs
# that clear inherited env vars still work) but a warning is emitted.
# -----------------------------------------------------------------------------


def test_default_venv_dir_warns_on_empty_zh_mcp_venv(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ZH_MCP_VENV", "")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    venv_dir, user_supplied = mcp_server._default_venv_dir()
    # Falls through to XDG default (NOT user-supplied — set-but-empty
    # is treated as unset).
    assert venv_dir == (tmp_path / "zh" / "venv").resolve()
    assert user_supplied is False
    # ...but warns to stderr so the user notices the empty value.
    err = capsys.readouterr().err
    assert "ZH_MCP_VENV" in err
    assert "empty" in err.lower()


def test_default_venv_dir_warns_on_whitespace_zh_mcp_venv(monkeypatch, capsys, tmp_path):
    # A common shell-quoting accident: ZH_MCP_VENV="$UNSET_VAR" expands
    # to empty; ZH_MCP_VENV="   " expands to whitespace. Both must warn.
    monkeypatch.setenv("ZH_MCP_VENV", "   ")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    mcp_server._default_venv_dir()
    err = capsys.readouterr().err
    assert "ZH_MCP_VENV" in err


# -----------------------------------------------------------------------------
# v1.7.0 — HOME unset detection (item 6)
# -----------------------------------------------------------------------------


def test_default_venv_dir_raises_when_home_unresolvable(monkeypatch):
    # macOS / Linux's expanduser falls back to pwd.getpwuid when HOME
    # is unset, so unsetting alone isn't sufficient to trigger the
    # detection. Pin the failure mode by pointing XDG_DATA_HOME at a
    # `~unknown-user-XXX` form that genuinely can't be resolved: per
    # POSIX, expanduser returns it unchanged, and our detection
    # catches the leading `~` and raises.
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "~no-such-user-zh-test/foo")
    with pytest.raises(RuntimeError, match="cannot resolve home directory"):
        mcp_server._default_venv_dir()


# -----------------------------------------------------------------------------
# v1.7.0 — Build sentinel acceptance (item 1)
# Sentinel-only dirs are recognized by _looks_like_zh_venv as ours,
# enabling self-recovery from a crashed mid-build.
# -----------------------------------------------------------------------------


def test_looks_like_zh_venv_accepts_sentinel_only_dir(tmp_path):
    # A previous _build_venv crashed after writing the sentinel but
    # before the marker. The dir has the sentinel + partial venv
    # contents but NO marker. _looks_like_zh_venv must accept it so
    # the next launch can clean up and rebuild.
    (tmp_path / mcp_server._BUILD_SENTINEL).write_text("deps_hash=x\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").touch(mode=0o755)
    (tmp_path / "lib").mkdir()  # partial install detritus
    assert mcp_server._looks_like_zh_venv(tmp_path)


def test_looks_like_zh_venv_still_refuses_foreign_venv_with_no_sentinel(tmp_path):
    # Foreign venv with pyvenv.cfg but no marker and no sentinel — must
    # still be refused (item 1 from round-2 must not regress).
    (tmp_path / "pyvenv.cfg").write_text("home = /fake\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").touch(mode=0o755)
    assert not mcp_server._looks_like_zh_venv(tmp_path)


# -----------------------------------------------------------------------------
# v1.7.0 — _ensure_safe_parent (items 3 + 4)
# -----------------------------------------------------------------------------


def test_ensure_safe_parent_accepts_existing_dir(tmp_path):
    # Parent already exists as a directory — proceed silently. Either
    # mode (user-supplied or server-default) accepts.
    parent = tmp_path / "existing"
    parent.mkdir()
    venv_dir = parent / "venv"
    mcp_server._ensure_safe_parent(venv_dir, user_supplied=True)
    mcp_server._ensure_safe_parent(venv_dir, user_supplied=False)


def test_ensure_safe_parent_user_supplied_creates_one_level(tmp_path):
    # User-supplied mode: grandparent exists, parent doesn't — create
    # ONE level only.
    venv_dir = tmp_path / "zh" / "venv"
    mcp_server._ensure_safe_parent(venv_dir, user_supplied=True)
    assert (tmp_path / "zh").is_dir()


def test_ensure_safe_parent_user_supplied_refuses_deep_creation(tmp_path):
    # Round-2 (still): user-supplied mode with missing grandparent
    # must refuse rather than auto-create deep tree. Round-3 fix:
    # no longer depends on env state — passed as parameter.
    venv_dir = tmp_path / "deep" / "nested" / "tree" / "venv"
    with pytest.raises(RuntimeError, match="ancestor"):
        mcp_server._ensure_safe_parent(venv_dir, user_supplied=True)
    assert not (tmp_path / "deep").exists()  # nothing was created


def test_ensure_safe_parent_server_default_creates_deep_tree(tmp_path):
    # Round-2 CRITICAL fix (still): server-chosen XDG default must
    # auto-create the ancestor tree on fresh macOS / minimal container.
    # Round-3 fix: explicit `user_supplied=False` (no env coupling).
    venv_dir = tmp_path / "share" / "zh" / "venv"
    assert not (tmp_path / "share").exists()
    mcp_server._ensure_safe_parent(venv_dir, user_supplied=False)
    assert (tmp_path / "share" / "zh").is_dir()


def test_ensure_safe_parent_refuses_regular_file_parent(tmp_path):
    # Stray file at the parent path raises a clear error in BOTH modes
    # (avoiding the unhelpful FileExistsError from mkdir).
    parent_file = tmp_path / "stray-file"
    parent_file.write_text("not a directory")
    venv_dir = parent_file / "venv"
    with pytest.raises(RuntimeError, match="is not a directory"):
        mcp_server._ensure_safe_parent(venv_dir, user_supplied=True)
    with pytest.raises(RuntimeError, match="is not a directory"):
        mcp_server._ensure_safe_parent(venv_dir, user_supplied=False)


def test_ensure_safe_parent_user_supplied_idempotent_on_concurrent_parent_create(tmp_path):
    # Round-3 #2: the user-supplied branch's `parent.mkdir(exist_ok=True)`
    # must tolerate a concurrent sibling process having already created
    # the parent dir between our `parent.exists()` check and the mkdir.
    # Simulate by pre-creating the parent.
    venv_dir = tmp_path / "zh" / "venv"
    (tmp_path / "zh").mkdir()  # "race winner" already created it
    # If exist_ok were missing, this would FileExistsError.
    mcp_server._ensure_safe_parent(venv_dir, user_supplied=True)
    assert (tmp_path / "zh").is_dir()


# -----------------------------------------------------------------------------
# v1.7.0 — _safe_rmtree (item 5)
# -----------------------------------------------------------------------------


def test_safe_rmtree_unlinks_symlinks_without_following(tmp_path):
    # _safe_rmtree on a symlink-to-dir must remove the LINK, not
    # recursively destroy the target (which is some user's real venv).
    target = tmp_path / "real-venv"
    target.mkdir()
    (target / "important.txt").write_text("don't delete me")
    link = tmp_path / "link-to-venv"
    link.symlink_to(target)
    mcp_server._safe_rmtree(link)
    assert not link.exists()
    assert target.is_dir()
    assert (target / "important.txt").read_text() == "don't delete me"


def test_safe_rmtree_removes_normal_directory(tmp_path):
    # Sanity: ordinary recursive delete still works.
    d = tmp_path / "venv"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("b")
    mcp_server._safe_rmtree(d)
    assert not d.exists()


# -----------------------------------------------------------------------------
# v1.7.0 — Probe split (item 8)
# Per-launch probe stays light; full probe (post-build) lists every dep.
# -----------------------------------------------------------------------------


def test_per_launch_probe_imports_only_mcp():
    # Per-launch probe stays at ~1s by importing only `mcp`. The
    # heavyweight torch / transformers import is reserved for the
    # post-build full probe.
    assert mcp_server._venv_per_launch_probe() == "import mcp"


def test_full_probe_imports_every_dep():
    probe = mcp_server._venv_full_probe()
    assert "import mcp" in probe
    assert "import sentence_transformers" in probe
    assert "import numpy" in probe


def test_full_probe_recomputes_from_venv_deps(monkeypatch):
    monkeypatch.setattr(mcp_server, "_VENV_DEPS", ("mcp", "numpy"))
    probe = mcp_server._venv_full_probe()
    assert probe == "import mcp; import numpy"


# --- version-pinned deps (the bug that took the MCP server down) -------------
# _VENV_DEPS entries are REQUIREMENT SPECIFIERS, not module names. The probes
# used a bare `name.replace('-', '_')`, so the moment a dep carried a version
# constraint the probe emitted `import mcp>=1.0,<2` — a SyntaxError. The probe
# then "failed", the freshly-built venv was judged broken and DELETED, and every
# launch rebuilt (~1GB) and failed identically. Pinning `mcp<2` was required
# (the 2.x SDK dropped `mcp.server.fastmcp`), so this path had to work.

def test_dep_module_name_strips_version_specifiers():
    assert mcp_server._dep_module_name("mcp>=1.0,<2") == "mcp"
    assert mcp_server._dep_module_name("numpy==1.26.4") == "numpy"
    assert mcp_server._dep_module_name("sentence-transformers") == "sentence_transformers"
    assert mcp_server._dep_module_name("pkg[extra]>=2") == "pkg"
    assert mcp_server._dep_module_name("torch ~= 2.0") == "torch"


def test_probes_emit_valid_python_for_pinned_deps(monkeypatch):
    """STRUCTURAL: every statement a probe emits must actually parse.

    Asserting on the exact string would have passed the buggy version too if
    someone wrote the expectation to match; parsing is what proves the probe can
    run at all.
    """
    import ast

    monkeypatch.setattr(
        mcp_server, "_VENV_DEPS", ("mcp>=1.0,<2", "sentence-transformers", "numpy==1.26.4")
    )
    full = mcp_server._venv_full_probe()
    for stmt in full.split("; "):
        ast.parse(stmt)  # raises SyntaxError on the pre-fix output
    ast.parse(mcp_server._venv_per_launch_probe())

    assert full == "import mcp; import sentence_transformers; import numpy"
    assert mcp_server._venv_per_launch_probe() == "import mcp"


def test_probe_never_emits_a_version_specifier(monkeypatch):
    """No specifier character may survive into an import statement."""
    monkeypatch.setattr(mcp_server, "_VENV_DEPS", ("mcp>=1.0,<2", "numpy==1.26.4"))
    probe = mcp_server._venv_full_probe()
    for ch in "<>=!~[]":
        assert ch not in probe, f"{ch!r} leaked into the probe: {probe!r}"


# -----------------------------------------------------------------------------
# v1.7.0 — _venv_build_lock (item 2)
# Smoke test: lock is acquired + released, no exceptions, lock file exists.
# Actual concurrency contention testing would need subprocess + fcntl
# coordination — out of scope for unit tests.
# -----------------------------------------------------------------------------


def test_venv_build_lock_acquires_and_releases(tmp_path):
    venv_dir = tmp_path / "zh-venv"
    lock_path = venv_dir.parent / mcp_server._BOOTSTRAP_LOCK
    with mcp_server._venv_build_lock(venv_dir, user_supplied=False):
        # Lock file exists during the with-block.
        assert lock_path.exists()
    # After release, the lock file remains (we don't clean it up — that
    # matches the documented "lockfile-as-persistent-coordinator" pattern).
    assert lock_path.exists()


def test_venv_build_lock_creates_parent_dir(tmp_path):
    # When grandparent exists, the lock context creates the parent
    # via `_ensure_safe_parent` so subsequent acquisitions don't fail.
    venv_dir = tmp_path / "new-parent" / "venv"
    assert not venv_dir.parent.exists()
    with mcp_server._venv_build_lock(venv_dir, user_supplied=False):
        assert venv_dir.parent.is_dir()


def test_venv_build_lock_refuses_typo_paths_before_mkdir(tmp_path):
    # PR #15 round-1 CRITICAL (still): the lock calls _ensure_safe_parent
    # BEFORE creating the lock file, so a typo'd ZH_MCP_VENV doesn't
    # materialize an ancestor tree. Round-3 fix: passes user_supplied
    # explicitly instead of re-reading env.
    venv_dir = tmp_path / "deep" / "nested" / "tree" / "venv"
    with pytest.raises(RuntimeError, match="ancestor"):
        with mcp_server._venv_build_lock(venv_dir, user_supplied=True):
            pass  # should not reach this
    # Nothing got auto-created on disk — the safety check fired early.
    assert not (tmp_path / "deep").exists()


def test_venv_build_lock_classification_decoupled_from_env(tmp_path, monkeypatch):
    # Round-3 #4: the lock's safety classification must NOT re-read
    # env. Verify by setting ZH_MCP_VENV='something-typo-y' but passing
    # user_supplied=False — the server-default permissive mode applies,
    # so a deep tree is created without complaint. Conversely, unsetting
    # the env but passing user_supplied=True forces the strict refusal.
    deep = tmp_path / "deep-tree" / "venv"
    monkeypatch.setenv("ZH_MCP_VENV", "/some/unrelated/typo/path/venv")
    # user_supplied=False overrides env — server-default mode auto-creates.
    with mcp_server._venv_build_lock(deep, user_supplied=False):
        pass
    assert deep.parent.is_dir()
    # Conversely:
    other_deep = tmp_path / "other-deep-tree" / "child" / "venv"
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    # user_supplied=True forces strict mode even though env is unset.
    with pytest.raises(RuntimeError, match="ancestor"):
        with mcp_server._venv_build_lock(other_deep, user_supplied=True):
            pass
    assert not (tmp_path / "other-deep-tree").exists()


def test_venv_build_lock_uses_o_nofollow(tmp_path):
    # Round-3 #3: lockfile fd is opened with O_NOFOLLOW so a planted
    # symlink at the lock path can't redirect the bootstrap's fd.
    venv_dir = tmp_path / "zh" / "venv"
    venv_dir.parent.mkdir(parents=True)
    decoy_target = tmp_path / "decoy-target"
    decoy_target.write_text("untouched")
    lock_path = venv_dir.parent / mcp_server._BOOTSTRAP_LOCK
    lock_path.symlink_to(decoy_target)
    # Acquiring the lock through the symlink must fail with ELOOP
    # (POSIX errno 62 / 40 depending on platform) — proving O_NOFOLLOW
    # is honored.
    with pytest.raises(OSError):
        with mcp_server._venv_build_lock(venv_dir, user_supplied=False):
            pass
    # The lock path is still a symlink — the failed open didn't
    # materialize a regular file there (which would imply O_NOFOLLOW
    # was stripped and the open silently followed the link, creating
    # the regular file at decoy_target). This is the assertion that
    # actually demonstrates redirect-prevention: code paths that only
    # `flock` without writing wouldn't observably affect `decoy_target`
    # either way, so checking decoy contents proves nothing.
    assert lock_path.is_symlink()


# -----------------------------------------------------------------------------
# v1.7.0 round-1 review — _safe_rmtree ignore_errors mode
# Cleanup-path callers (inside _build_venv except blocks) need
# best-effort semantics: a cleanup failure shouldn't mask the original
# build error.
# -----------------------------------------------------------------------------


def test_safe_rmtree_ignore_errors_swallows_failures(tmp_path):
    # _safe_rmtree on a symlink whose unlink fails — ignore_errors=True
    # swallows. Use a non-existent path to provoke a clean FileNotFoundError
    # on the symlink unlink (closest portable approximation).
    bogus = tmp_path / "does-not-exist"
    # No-op for a missing path — no raise either way.
    mcp_server._safe_rmtree(bogus, ignore_errors=True)


def test_safe_rmtree_default_mode_raises(tmp_path):
    # Without ignore_errors, errors propagate (so production callers
    # that DO want to know about cleanup failures still see them).
    bogus = tmp_path / "regular-file"
    bogus.write_text("file, not a dir")
    # rmtree on a non-dir non-symlink raises NotADirectoryError on
    # POSIX (an OSError subclass).
    with pytest.raises(OSError):
        mcp_server._safe_rmtree(bogus)


def test_safe_rmtree_does_not_chmod_above_rmtree_root(tmp_path, monkeypatch):
    # Round-3 #1 (CRITICAL) regression pin. When `shutil.rmtree` fails
    # at the rmtree ROOT (`os.scandir(venv_dir)` raises), the round-2
    # implementation chmod'd `os.path.dirname(p) == venv_dir.parent`
    # — a directory OUTSIDE the cleanup target. For the default path
    # that silently downgraded `~/.local/share/zh` from 0o755 → 0o700;
    # for shared `ZH_MCP_VENV=/srv/shared/...` configs, it locked
    # group/world out of the shared parent. Round-3's fix bound
    # `rmtree_root` in the closure and gated the parent-chmod on
    # `p_parent == rmtree_root or rmtree_root in p_parent.parents`.
    #
    # This test simulates the EXACT failure mode (force scandir to
    # raise PermissionError on venv_dir once, intercept all chmod
    # calls, assert no chmod target sits at or above the rmtree root).
    # A future "simplification" that reverts the closure-bound check
    # to `os.chmod(os.path.dirname(p), 0o700)` re-introduces the
    # regression and this test fails.
    venv_dir = tmp_path / "share" / "zh" / "venv"
    venv_dir.mkdir(parents=True)
    (venv_dir / "child.txt").write_text("payload")

    chmod_targets: list[Path] = []
    real_chmod = os.chmod

    def chmod_spy(p, mode, *args, **kwargs):
        # `os.chmod` accepts (path, mode) AND (fd, mode); shutil may
        # use the fd form internally. Only record path-like calls;
        # always delegate to the real chmod.
        if isinstance(p, (str, os.PathLike)):
            chmod_targets.append(Path(p))
        return real_chmod(p, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod_spy)

    real_scandir = os.scandir
    fired_once = [False]

    def flaky_scandir(p):
        # Force the rmtree-root EACCES exactly once. shutil's
        # `_rmtree_safe_fd` opens venv_dir as a file descriptor then
        # calls os.scandir(fd) — so `p` here may be an int fd, not a
        # path. The first scandir IS the root call, so just fail on
        # the very first invocation rather than trying to inspect `p`.
        if not fired_once[0]:
            fired_once[0] = True
            raise PermissionError(13, "EACCES")
        return real_scandir(p)

    monkeypatch.setattr(os, "scandir", flaky_scandir)

    # Best-effort mode so a failure to recover doesn't mask the
    # property under test.
    mcp_server._safe_rmtree(venv_dir, ignore_errors=True)

    # Critical invariant: no chmod target may be AT or ABOVE the
    # rmtree root's PARENT. Equivalent: every chmod target must be
    # at-or-below the rmtree root.
    forbidden = {
        tmp_path,
        tmp_path / "share",
        tmp_path / "share" / "zh",  # this is rmtree_root.parent
    }
    leaked = forbidden & set(chmod_targets)
    assert not leaked, (
        f"_safe_rmtree chmod'd above the rmtree root: {sorted(leaked)} "
        f"(all chmod calls: {chmod_targets})"
    )


# -----------------------------------------------------------------------------
# v1.7.2 — reject relative ZH_MCP_VENV (#5)
# -----------------------------------------------------------------------------


def test_default_venv_dir_rejects_relative_zh_mcp_venv(monkeypatch):
    # `./venv` would .resolve() against the launch cwd → a different
    # venv per project → orphaned ~500MB trees. Must raise.
    monkeypatch.setenv("ZH_MCP_VENV", "./venv")
    with pytest.raises(RuntimeError, match="absolute"):
        mcp_server._default_venv_dir()


def test_default_venv_dir_rejects_bare_relative_zh_mcp_venv(monkeypatch):
    monkeypatch.setenv("ZH_MCP_VENV", "some/relative/venv")
    with pytest.raises(RuntimeError, match="absolute"):
        mcp_server._default_venv_dir()


def test_default_venv_dir_accepts_absolute_zh_mcp_venv(monkeypatch, tmp_path):
    # Sanity: an absolute path still works and is flagged user_supplied.
    target = tmp_path / "abs-venv"
    monkeypatch.setenv("ZH_MCP_VENV", str(target))
    venv_dir, user_supplied = mcp_server._default_venv_dir()
    assert venv_dir == target.resolve()
    assert user_supplied is True


# -----------------------------------------------------------------------------
# v1.7.2 — probe timeout env override (#3)
# -----------------------------------------------------------------------------


def test_probe_timeout_default_is_30(monkeypatch):
    monkeypatch.delenv("ZH_MCP_PROBE_TIMEOUT", raising=False)
    assert mcp_server._probe_timeout_default() == 30


def test_probe_timeout_env_override(monkeypatch):
    monkeypatch.setenv("ZH_MCP_PROBE_TIMEOUT", "120")
    assert mcp_server._probe_timeout_default() == 120


def test_probe_timeout_invalid_env_falls_back_to_30(monkeypatch):
    for bad in ("", "  ", "abc", "-5", "0", "12.5"):
        monkeypatch.setenv("ZH_MCP_PROBE_TIMEOUT", bad)
        assert mcp_server._probe_timeout_default() == 30, f"bad value {bad!r}"


# -----------------------------------------------------------------------------
# v1.7.2 — per-launch probe derived from _VENV_DEPS[0] (#7)
# -----------------------------------------------------------------------------


def test_per_launch_probe_derives_from_first_dep(monkeypatch):
    assert mcp_server._venv_per_launch_probe() == "import mcp"
    # A rename/reorder of the deps tuple must be reflected (no hardcode).
    monkeypatch.setattr(mcp_server, "_VENV_DEPS", ("some-pkg", "numpy"))
    assert mcp_server._venv_per_launch_probe() == "import some_pkg"


# -----------------------------------------------------------------------------
# v1.7.2 — corrupted-deps error gives an actionable rebuild hint (#2)
# -----------------------------------------------------------------------------


def test_similarity_exc_to_stderr_module_not_found_gives_rebuild_hint(monkeypatch):
    monkeypatch.setenv("ZH_MCP_VENV", "/tmp/zh-test-venv")
    msg = mcp_server._similarity_exc_to_stderr(
        ModuleNotFoundError("No module named 'sentence_transformers'")
    )
    assert "rm -rf" in msg
    assert "/tmp/zh-test-venv" in msg
    assert "rebuild" in msg.lower()


def test_similarity_exc_to_stderr_passthrough_for_other_errors():
    # Non-import errors are surfaced verbatim (no misleading rebuild hint).
    msg = mcp_server._similarity_exc_to_stderr(ValueError("bad query"))
    assert msg == "bad query"
    assert "rm -rf" not in msg


# ---------------------------------------------------------------------------
# v1.9.0 round-3 MCP fixes (PR #23 findings #1, #3, #4, #11).
# ---------------------------------------------------------------------------


def test_with_epic_number_alias_promotes_number():
    """epic_* responses returning `number` carry an `epic_number` alias for
    v1.8.x back-compat (review finding #1).
    """
    d = mcp_server._with_epic_number_alias(
        {"ok": True, "number": 42, "url": "u"}
    )
    assert d["epic_number"] == 42
    assert d["number"] == 42
    assert d["url"] == "u"


def test_with_epic_number_alias_promotes_parent():
    """epic_add_children / epic_remove_children return `parent`, so the
    alias falls back to that (the epic IS the parent in those responses).
    """
    d = mcp_server._with_epic_number_alias(
        {"ok": True, "parent": 100, "added": [1]}
    )
    assert d["epic_number"] == 100
    assert d["parent"] == 100


def test_with_epic_number_alias_respects_explicit_value():
    """An explicit `epic_number` already present is not overwritten."""
    d = mcp_server._with_epic_number_alias(
        {"number": 1, "epic_number": 999}
    )
    assert d["epic_number"] == 999


def test_with_epic_number_alias_handles_missing_identifier():
    """Error returns (no `number` / `parent`) get `epic_number: None`.

    v1.9.1 round-3 #3: the round-2 #6 minimization of the
    _planning_create blocked-response dropped both number and parent
    placeholders, so without this fallback a v1.8.x agent reading
    `out["epic_number"]` on a blocked create would get KeyError. Always
    ensure the key is present.
    """
    d = mcp_server._with_epic_number_alias({"ok": False, "stderr": "oops"})
    assert "epic_number" in d
    assert d["epic_number"] is None


def test_planning_create_forwards_new_kwargs(monkeypatch):
    """_planning_create must pass assignee / estimate / parent as `-a`,
    `-e`, `--parent` to the bash side. Review finding #3.
    """
    captured = {}

    def fake_run_zh(args, cwd=None):
        captured["args"] = list(args)
        return {
            "ok": True,
            "stdout_plain": (
                '{"number": 42, "url": "https://example/42",'
                ' "title": "T", "type": "Epic",'
                ' "pipeline": "Backlog", "estimate": 5, "parent": 100}'
            ),
            "stderr": "",
        }

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    out = mcp_server._planning_create(
        "epic", "T", "body", "label1,label2", "Backlog",
        "alice", "5", 100, "",
    )
    assert out["ok"] is True
    args = captured["args"]
    assert args[:4] == ["epic", "create", "T", "--json"]
    assert "-d" in args and "body" in args
    assert "-l" in args and "label1,label2" in args
    assert "-p" in args and "Backlog" in args
    assert "-a" in args and "alice" in args
    assert "-e" in args and "5" in args
    assert "--parent" in args and "100" in args


def test_planning_create_omits_unset_kwargs(monkeypatch):
    """Empty / zero kwargs should not appear in the argv so a caller
    passing assignee="" does not end up with a stray `-a ""`.
    """
    captured = {}

    def fake_run_zh(args, cwd=None):
        captured["args"] = list(args)
        return {
            "ok": True,
            "stdout_plain": '{"number": 1}',
            "stderr": "",
        }

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    mcp_server._planning_create(
        "epic", "T", "", "", "", "", "", 0, "",
    )
    args = captured["args"]
    assert "-a" not in args and "-e" not in args
    assert "--parent" not in args
    assert "-d" not in args and "-l" not in args and "-p" not in args


def test_planning_create_forwards_pipeline_and_parent_in_response(monkeypatch):
    """_planning_create's return shape includes `pipeline` and `parent`
    parsed from the JSON instead of dropping them. Review finding #4.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": (
                '{"number": 42, "url": "u", "title": "T", "type": "Epic",'
                ' "pipeline": "Backlog", "estimate": 5, "parent": 100}'
            ),
            "stderr": "",
        },
    )
    out = mcp_server._planning_create(
        "epic", "T", "", "", "Backlog", "", "", 100, "",
    )
    assert out["number"] == 42
    assert out["pipeline"] == "Backlog"
    assert out["parent"] == 100
    assert out["estimate"] == 5
    assert out["type"] == "Epic"


def test_epic_list_returns_both_items_and_epics(monkeypatch):
    """epic_list exposes the new noun-neutral `items` key AND a
    back-compat `epics` alias. Review finding #11.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": (
                "  #42  OPEN    Auth redesign\n"
                "  #99  CLOSED  Old auth\n"
            ),
            "stderr": "",
        },
    )
    out = mcp_server.epic_list("")
    assert "items" in out and "epics" in out
    assert out["items"] == out["epics"]
    nums = {x["number"] for x in out["items"]}
    assert nums == {42, 99}


def test_planning_list_returns_items_only(monkeypatch):
    """Non-epic _planning_list (initiative/project/subtask) returns
    `items` WITHOUT the `epics` alias. Confirms the alias is
    epic_list-specific.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": "  #1  OPEN  An initiative\n",
            "stderr": "",
        },
    )
    out = mcp_server._planning_list("initiative", "")
    assert "items" in out
    assert "epics" not in out


# ---------------------------------------------------------------------------
# v1.9.1 item #5: duplicate-check pre-flight on planning-noun creates.
# `_planning_create` now runs the same check that `create_issue` uses, so an
# agent calling `epic_create("Auth redesign")` gets blocked on a near-
# duplicate the same way `create_issue(..., type="Epic")` would.
# ---------------------------------------------------------------------------


def test_planning_create_blocks_on_duplicate(monkeypatch):
    """A `recommendation == "block"` response from check_duplicate must
    short-circuit the create. The bash side never runs; the caller sees
    ok=False with the candidate matches and a clear retry hint.
    """
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: ("owner/repo", None),
    )

    blocked_info = {
        "ok": True,
        "recommendation": "block",
        "hard_threshold": 0.7,
        "matches": [
            {"number": 42, "title": "Auth redesign", "similarity": 0.85},
        ],
    }
    import similarity as _similarity_module
    monkeypatch.setattr(
        _similarity_module, "check_duplicate",
        lambda title, body, repo, **kwargs: blocked_info,
    )

    called = {"ran": False}

    def fake_run_zh(args, cwd=None):
        called["ran"] = True
        return {"ok": True, "stdout_plain": '{"number": 99}', "stderr": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)

    out = mcp_server._planning_create(
        "epic", "Auth redesign", "body", "", "", "", "", 0, "",
    )
    assert out["ok"] is False
    assert out.get("blocked") is True
    # v1.9.2 round-7 finding #7: the block response now carries the
    # full documented key set with None placeholders so initiative /
    # project / subtask docstrings (which list `number, url, type,
    # pipeline, parent, estimate, raw, stderr, duplicate_check`) hold
    # for the blocked path too. Round-2 finding #6's minimal-4-key
    # shape was reverted because it made clients KeyError on the
    # documented contract.
    for key in ("number", "url", "type", "pipeline", "parent",
                "estimate", "estimate_requested", "raw"):
        assert key in out, f"blocked response missing {key!r}"
        assert out[key] in (None, ""), (
            f"blocked-path {key!r} should be None/empty, got {out[key]!r}"
        )
    assert out["duplicate_check"] == blocked_info
    assert "confirm_create=True" in out["stderr"]
    assert called["ran"] is False, "bash create must NOT run when blocked"


def test_planning_create_confirm_create_overrides_block(monkeypatch):
    """`confirm_create=True` lets the create proceed even when the
    pre-flight reports a block, mirroring `create_issue`.
    """
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: ("owner/repo", None),
    )
    import similarity as _similarity_module
    monkeypatch.setattr(
        _similarity_module, "check_duplicate",
        lambda title, body, repo, **kwargs: {
            "ok": True,
            "recommendation": "block",
            "hard_threshold": 0.7,
            "matches": [{"number": 42, "title": "X", "similarity": 0.9}],
        },
    )

    def fake_run_zh(args, cwd=None):
        return {
            "ok": True,
            "stdout_plain": (
                '{"number": 99, "url": "u", "title": "T",'
                ' "type": "Epic", "pipeline": null, "estimate": null,'
                ' "parent": null}'
            ),
            "stderr": "",
        }

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)

    out = mcp_server._planning_create(
        "epic", "Auth redesign", "body", "", "", "", "", 0, "",
        confirm_create=True,
    )
    assert out["ok"] is True
    assert out["number"] == 99
    # duplicate_check is still surfaced so the caller can audit the
    # override decision.
    assert out["duplicate_check"]["recommendation"] == "block"


def test_planning_create_skip_duplicate_check_bypasses_preflight(monkeypatch):
    """`skip_duplicate_check=True` skips the pre-flight similarity
    machinery entirely (the similarity layer is never consulted).

    v1.9.2 round-4 (PR #27) finding #9: the docstring contract
    promises `duplicate_check` is always present on every successful
    return. When the pre-flight is bypassed, the key carries a
    `{"recommendation": "skipped", "matches": []}` placeholder so
    clients reading the field uniformly do not KeyError.
    """
    sim_called = {"ran": False}

    def fake_similarity_repo(repo_path):
        sim_called["ran"] = True
        return ("owner/repo", None)

    monkeypatch.setattr(
        mcp_server, "_similarity_repo", fake_similarity_repo,
    )

    def fake_run_zh(args, cwd=None):
        return {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T"}',
            "stderr": "",
        }

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)

    out = mcp_server._planning_create(
        "epic", "X", "body", "", "", "", "", 0, "",
        skip_duplicate_check=True,
    )
    assert out["ok"] is True
    # The key is now ALWAYS present (round-4 #9); the placeholder
    # distinguishes "skipped" from a real recommendation.
    assert "duplicate_check" in out
    assert out["duplicate_check"] == {"recommendation": "skipped",
                                      "matches": []}
    # And the similarity machinery still wasn't invoked.
    assert sim_called["ran"] is False


def test_planning_create_warn_recommendation_does_not_block(monkeypatch):
    """A non-block recommendation (e.g. soft match below the hard
    threshold) must annotate the response without short-circuiting
    the create.
    """
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: ("owner/repo", None),
    )
    import similarity as _similarity_module
    monkeypatch.setattr(
        _similarity_module, "check_duplicate",
        lambda title, body, repo, **kwargs: {
            "ok": True,
            "recommendation": "warn",
            "soft_threshold": 0.5,
            "matches": [{"number": 7, "title": "loosely related",
                         "similarity": 0.55}],
        },
    )

    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T"}',
            "stderr": "",
        },
    )

    out = mcp_server._planning_create(
        "epic", "X", "body", "", "", "", "", 0, "",
    )
    assert out["ok"] is True
    assert out["number"] == 99
    assert out["duplicate_check"]["recommendation"] == "warn"


def test_planning_create_similarity_failure_does_not_block(monkeypatch):
    """If the similarity layer errors (missing repo, embedding deps
    unavailable, etc.) the create still proceeds. The error is logged
    in duplicate_check.stderr; an infra outage in similarity must not
    become a planning-noun outage.
    """
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: (None, "could not derive repo"),
    )
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T"}',
            "stderr": "",
        },
    )
    out = mcp_server._planning_create(
        "epic", "X", "body", "", "", "", "", 0, "",
    )
    assert out["ok"] is True
    assert out["number"] == 99
    assert out["duplicate_check"]["matches"] == []
    assert "could not derive repo" in out["duplicate_check"]["stderr"]


# ---------------------------------------------------------------------------
# v1.9.7 (#52): the create surfaces must FORWARD `parent` and `related_issues`
# to check_duplicate so the #46 structural-relative downgrade is reachable
# from a bulk-load caller. The check_duplicate(related_issues=...) downgrade
# itself is unit-tested in test_similarity_structural.py; these pin the
# forwarding seam (create_issue / _planning_create -> check_duplicate), which
# nothing exercised before.
# ---------------------------------------------------------------------------


def _capture_check_duplicate(monkeypatch, *, result):
    """Stub similarity.check_duplicate to record the kwargs it receives and
    return `result`. Returns the captured-calls list."""
    captured = []

    def fake_check_duplicate(title, body, repo, **kwargs):
        captured.append(kwargs)
        return result

    import similarity as _similarity_module
    monkeypatch.setattr(
        _similarity_module, "check_duplicate", fake_check_duplicate,
    )
    return captured


# A structural-relative warn result: a hard match against a relative that #46
# downgraded from block to warn. The create must proceed (warn doesn't block).
_STRUCTURAL_WARN = {
    "ok": True,
    "recommendation": "warn",
    "any_above_hard": True,
    "any_above_soft": True,
    "downgraded_structural": True,
    "hard_threshold": 0.7,
    "soft_threshold": 0.55,
    "matches": [
        {"number": 7, "title": "dep", "similarity": 0.74,
         "match_kind": "structural_relative"},
    ],
}


def test_create_issue_forwards_parent_and_related_issues(monkeypatch):
    """create_issue must pass `parent` (when > 0) and `related_issues`
    through to check_duplicate, and a structural-relative warn must NOT
    block the create."""
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: ("owner/repo", None),
    )
    captured = _capture_check_duplicate(monkeypatch, result=_STRUCTURAL_WARN)
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T"}',
            "stderr": "",
        },
    )

    out = mcp_server.create_issue(
        title="Pydantic response feature", body="depends on the projections",
        parent=4, related_issues=[7, 8],
    )

    assert len(captured) == 1, "check_duplicate must be called exactly once"
    assert captured[0].get("parent") == 4, (
        f"create_issue must forward parent; got {captured[0]!r}"
    )
    assert captured[0].get("related_issues") == [7, 8], (
        f"create_issue must forward related_issues; got {captured[0]!r}"
    )
    # warn (not block) -> the create proceeds and the downgrade is surfaced.
    assert out["ok"] is True
    assert out["duplicate_check"]["recommendation"] == "warn"
    assert out["duplicate_check"]["downgraded_structural"] is True


def test_create_issue_parent_zero_forwards_none(monkeypatch):
    """`parent=0` (the no-parent default) must forward as `parent=None`,
    not the literal 0, so check_duplicate doesn't treat issue #0 as a
    structural relative."""
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: ("owner/repo", None),
    )
    captured = _capture_check_duplicate(
        monkeypatch,
        result={"ok": True, "recommendation": "create", "matches": []},
    )
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T"}',
            "stderr": "",
        },
    )

    mcp_server.create_issue(title="T", body="b")  # parent defaults to 0

    assert captured[0].get("parent") is None, (
        f"parent=0 must forward as None, not 0; got {captured[0]!r}"
    )
    assert captured[0].get("related_issues") is None


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_forwards_related_issues(monkeypatch, noun_create):
    """Each planning-noun create must forward `parent` and `related_issues`
    to check_duplicate.

    v1.9.7 (#53 review #5): the `related_issues=related_issues` forwarding
    lives in each of the four wrappers (epic/initiative/project/subtask),
    NOT in the shared _planning_create body. A dropped kwarg in one wrapper
    would slip through if only epic_create were tested, so parametrize over
    all four.
    """
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: ("owner/repo", None),
    )
    captured = _capture_check_duplicate(monkeypatch, result=_STRUCTURAL_WARN)
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            # parent=4 matches the requested parent, so this models a CLEAN
            # success (partial_applied stays False). Omitting parent here
            # would silently trip the v1.9.8 wire-failure branch.
            "stdout_plain": '{"number": 99, "url": "u", "title": "T", "parent": 4}',
            "stderr": "",
        },
    )

    out = getattr(mcp_server, noun_create)(
        title="Pydantic response item", description="depends on projections",
        parent=4, related_issues=[7],
    )

    assert captured[0].get("parent") == 4, (
        f"{noun_create} must forward parent; got {captured[0]!r}"
    )
    assert captured[0].get("related_issues") == [7], (
        f"{noun_create} must forward related_issues; got {captured[0]!r}"
    )
    assert out["ok"] is True
    assert out["partial_applied"] is False
    assert out["duplicate_check"]["downgraded_structural"] is True


# ---------------------------------------------------------------------------
# v1.9.8 (#54): _planning_create must detect parent-wire (addSubIssues)
# failure and report partial_applied=True, mirroring create_issue, so the
# planning-noun creates honor the uniform partial_applied contract and the
# bulk-load orphan guard works through them too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_parent_wire_failure_sets_partial_applied(
        monkeypatch, noun_create):
    """Create succeeded (number returned) but the issue is NOT under the
    requested parent (addSubIssues failed -> parent=null): partial_applied
    must be True so a caller treats it as a wire-failure, not a clean
    success."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            # number present, but parent came back null despite --parent 4
            "stdout_plain": '{"number": 99, "url": "u", "title": "T", "parent": null}',
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(
        title="T", parent=4, skip_duplicate_check=True,
    )
    assert out["ok"] is True
    assert out["number"] == 99
    assert out["partial_applied"] is True, (
        f"{noun_create}: parent-wire failure (requested 4, got null) must "
        f"set partial_applied=True; got {out!r}"
    )
    # The wire-failure path must still carry the full documented key set
    # (precedent: v1.9.2 round-7 #7 trimmed the blocked-path keys to 4 and
    # KeyErrored strict clients). A future trim here would slip past a
    # partial_applied-only assertion.
    for key in ("number", "url", "type", "pipeline", "parent", "estimate",
                "estimate_requested", "priority", "priority_requested",
                "raw", "stderr", "duplicate_check"):
        assert key in out, f"{noun_create} wire-failure shape missing {key!r}"
    if noun_create == "epic_create":
        # back-compat alias added by _with_epic_number_alias; pin it on the
        # partial path too so a dropped setdefault is caught.
        assert "epic_number" in out


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_parent_mismatch_is_partial(monkeypatch, noun_create):
    """A non-null returned parent that differs from the requested one
    (requested 4, got 7) is also a wire failure -> partial_applied True.

    Defensive coverage of the `!=` branch with both sides non-null. Bash
    `cmd_create` cannot actually produce a non-null mismatched parent (it
    emits either the requested parent or null), so this pins the Python
    boolean, not a reachable production path."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T", "parent": 7}',
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(
        title="T", parent=4, skip_duplicate_check=True,
    )
    assert out["ok"] is True
    assert out["partial_applied"] is True


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_parse_failure_is_not_partial(monkeypatch, noun_create):
    """ok=True but unparseable stdout -> created is None: ok collapses to
    False and partial_applied must stay False (a hard parse failure is not
    a partial-success, so the bulk-load guard shouldn't read it as one)."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": "not json at all",
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(
        title="T", parent=4, skip_duplicate_check=True,
    )
    assert out["ok"] is False
    assert out["partial_applied"] is False


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_parent_wired_ok_is_not_partial(monkeypatch, noun_create):
    """When the issue lands under the requested parent, partial_applied is
    False (no false positive on the happy path)."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T", "parent": 4}',
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(
        title="T", parent=4, skip_duplicate_check=True,
    )
    assert out["ok"] is True
    assert out["partial_applied"] is False


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_no_parent_requested_is_not_partial(
        monkeypatch, noun_create):
    """No parent requested -> a null returned parent is expected, not a
    wire failure."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T", "parent": null}',
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(title="T", skip_duplicate_check=True)
    assert out["ok"] is True
    assert out["partial_applied"] is False


# ---------------------------------------------------------------------------
# v1.9.9 (#61): the four planning-noun creates accept `priority` inline,
# forwarded as --priority to the bash noun-create (which delegates to
# cmd_create), mirroring create_issue. Priority failure surfaces via the
# priority / priority_requested divergence, NOT partial_applied.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_forwards_priority_flag(monkeypatch, noun_create):
    """`priority=` must be forwarded to bash as `--priority <name>`."""
    captured = {}
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: captured.update(args=args) or {
            "ok": True,
            "stdout_plain": ('{"number": 99, "url": "u", "title": "T", '
                             '"priority": "High", "priority_requested": "High"}'),
            "stderr": "",
        },
    )
    getattr(mcp_server, noun_create)(
        title="T", priority="High", skip_duplicate_check=True,
    )
    args = captured["args"]
    assert "--priority" in args, (
        f"{noun_create} must forward --priority; got {args!r}"
    )
    assert args[args.index("--priority") + 1] == "High"


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_priority_applied_success(monkeypatch, noun_create):
    """On a confirmed priority, priority == priority_requested and
    partial_applied stays False (priority is not a wire signal)."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": ('{"number": 99, "url": "u", "title": "T", '
                             '"priority": "High", "priority_requested": "High"}'),
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(
        title="T", priority="High", skip_duplicate_check=True,
    )
    assert out["ok"] is True
    assert out["priority"] == "High"
    assert out["priority_requested"] == "High"
    assert out["partial_applied"] is False


@pytest.mark.parametrize("noun_create", [
    "epic_create", "initiative_create", "project_create", "subtask_create",
])
def test_planning_create_priority_divergence_not_partial(monkeypatch, noun_create):
    """Priority requested but not confirmed (priority=null,
    priority_requested=High): the three-state divergence is the failure
    signal — partial_applied must NOT flip (parity with create_issue,
    where partial_applied is the parent-wire signal only)."""
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "stdout_plain": ('{"number": 99, "url": "u", "title": "T", '
                             '"priority": null, "priority_requested": "High"}'),
            "stderr": "",
        },
    )
    out = getattr(mcp_server, noun_create)(
        title="T", priority="High", skip_duplicate_check=True,
    )
    assert out["ok"] is True
    assert out["priority"] is None
    assert out["priority_requested"] == "High"
    assert out["partial_applied"] is False, (
        f"{noun_create}: a priority that didn't confirm must surface via the "
        f"priority/priority_requested divergence, not partial_applied; "
        f"got {out!r}"
    )


def test_planning_create_no_priority_does_not_forward_flag(monkeypatch):
    """No priority requested -> no --priority in the bash args, and both
    priority fields are null (not-requested state)."""
    captured = {}
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: captured.update(args=args) or {
            "ok": True,
            "stdout_plain": ('{"number": 99, "url": "u", "title": "T", '
                             '"priority": null, "priority_requested": null}'),
            "stderr": "",
        },
    )
    out = mcp_server.epic_create(title="T", skip_duplicate_check=True)
    assert "--priority" not in captured["args"]
    assert out["priority"] is None
    assert out["priority_requested"] is None


# ---------------------------------------------------------------------------
# v1.9.1 round-6 fixes: MCP-side surface (PR #25 round-5 findings #4 and #6).
# ---------------------------------------------------------------------------


def test_set_issue_type_exposes_partial_applied_on_exit_2(monkeypatch):
    """Round-6 finding #4: when `zh type` exits 2 (the divergence-only
    partial convention added in this round), the MCP wrapper must
    surface `partial_applied: True` so an agent does not retry a
    change that already landed. `ok` flips to True under this
    condition because the type DID land.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": False,
            "exit_code": 2,
            "stdout_plain": "",
            "stderr": "WARN: Partially applied: type change on #42 landed...",
        },
    )
    out = mcp_server.set_issue_type(42, "Epic")
    assert out["ok"] is True
    assert out["partial_applied"] is True
    assert "Partially applied" in out["stderr"]


def test_set_issue_type_clean_success_partial_false(monkeypatch):
    """Regression guard: a clean success (exit 0) keeps
    partial_applied=False. The flag is only set on the divergence
    code, not on every success.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "exit_code": 0,
            "stdout_plain": "OK: Set type of #42 to Epic",
            "stderr": "",
        },
    )
    out = mcp_server.set_issue_type(42, "Epic")
    assert out["ok"] is True
    assert out["partial_applied"] is False


def test_set_issue_type_real_failure_partial_false(monkeypatch):
    """A real failure (exit 1, e.g. unknown type) keeps
    partial_applied=False so an agent CAN safely retry with a
    corrected input. `ok` stays False because nothing landed.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": False,
            "exit_code": 1,
            "stdout_plain": "",
            "stderr": "Issue type 'BadType' not found",
        },
    )
    out = mcp_server.set_issue_type(42, "BadType")
    assert out["ok"] is False
    assert out["partial_applied"] is False


def test_create_issue_threads_priority_to_args(monkeypatch):
    """Round-6 finding #6: `create_issue` now accepts a `priority`
    arg and forwards it to `zh create` as `--priority <name>`,
    mirroring the bash flag added in v1.9.1 item #8.
    """
    captured = {}

    def fake_run_zh(args, cwd=None):
        captured["args"] = list(args)
        return {
            "ok": True,
            "exit_code": 0,
            "stdout_plain": (
                '{"number": 99, "url": "u", "title": "T",'
                ' "type": "Bug", "pipeline": "Backlog",'
                ' "estimate": null, "parent": null,'
                ' "priority": "High", "priority_requested": "High"}'
            ),
            "stderr": "",
        }

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: (None, "skip"),
    )

    out = mcp_server.create_issue(
        "T", "body", type="Bug", priority="High",
        skip_duplicate_check=True,
    )
    assert out["ok"] is True
    assert "--priority" in captured["args"]
    pri_idx = captured["args"].index("--priority")
    assert captured["args"][pri_idx + 1] == "High"


def test_create_issue_shape_includes_priority_and_estimate(monkeypatch):
    """Round-6 finding #6 (shape parity): create_issue's response
    now carries `priority`, `priority_requested`, and `estimate`
    (the keys that already exist on _planning_create's response).
    MCP clients reading either entry point see the same key set.
    """
    monkeypatch.setattr(
        mcp_server, "_run_zh",
        lambda args, cwd=None: {
            "ok": True,
            "exit_code": 0,
            "stdout_plain": (
                '{"number": 99, "url": "u", "title": "T",'
                ' "type": "Bug", "pipeline": "Backlog",'
                ' "estimate": 5, "parent": null,'
                ' "priority": "High", "priority_requested": "High"}'
            ),
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: (None, "skip"),
    )

    out = mcp_server.create_issue(
        "T", "body", type="Bug", priority="High",
        skip_duplicate_check=True,
    )
    assert out["estimate"] == 5
    assert out["priority"] == "High"
    assert out["priority_requested"] == "High"


def test_create_issue_priority_omitted_when_not_passed(monkeypatch):
    """A create_issue call without `priority` must NOT add
    `--priority` to the argv (no stray empty-value pass-through).
    """
    captured = {}

    def fake_run_zh(args, cwd=None):
        captured["args"] = list(args)
        return {
            "ok": True,
            "exit_code": 0,
            "stdout_plain": '{"number": 99, "url": "u", "title": "T"}',
            "stderr": "",
        }

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    monkeypatch.setattr(
        mcp_server, "_similarity_repo",
        lambda repo_path: (None, "skip"),
    )
    mcp_server.create_issue(
        "T", "body", skip_duplicate_check=True,
    )
    assert "--priority" not in captured["args"]
