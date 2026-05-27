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
    assert mcp_server._default_venv_dir() == custom.resolve()


def test_default_venv_dir_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert mcp_server._default_venv_dir() == (tmp_path / "zh" / "venv").resolve()


def test_default_venv_dir_fallback(monkeypatch):
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = (
        Path(os.path.expanduser("~/.local/share")) / "zh" / "venv"
    ).resolve()
    assert mcp_server._default_venv_dir() == expected


def test_default_venv_dir_resolves_relative_xdg(monkeypatch, tmp_path):
    # Relative XDG_DATA_HOME must be pinned to an absolute path at
    # startup, otherwise launches from different cwds would compute
    # different venv locations and rebuild-loop.
    monkeypatch.delenv("ZH_MCP_VENV", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", "local-data")
    result = mcp_server._default_venv_dir()
    assert result.is_absolute()
    assert result == (tmp_path / "local-data" / "zh" / "venv").resolve()


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
# _parse_new_issue_number / _parse_new_epic_number — round-4 #3.
# Anchor on the ✓ success marker so an adversarial title containing
# `Created issue #NN` (or the preceding `Info: Creating issue: ...`
# line) doesn't trick the parser into returning the wrong number.
# -----------------------------------------------------------------------------


def test_parse_new_issue_number_returns_the_success_line_number():
    stdout = (
        "Info: Creating issue: Bug: Created issue #99 has wrong fix...\n"
        "✓ Created issue #42: Bug: Created issue #99 has wrong fix\n"
    )
    assert mcp_server._parse_new_issue_number(stdout) == 42


def test_parse_new_issue_number_returns_none_without_success_line():
    # No ✓ line means the create command never reported success — must
    # return None, not whatever number the Info line echoed from the
    # user's title.
    stdout = (
        "Info: Creating issue: Bug: Created issue #99 has wrong fix...\n"
        "Error: GraphQL mutation failed\n"
    )
    assert mcp_server._parse_new_issue_number(stdout) is None


def test_parse_new_epic_number_returns_the_success_line_number():
    stdout = (
        "Info: Creating epic: Bug: see Created epic #99 for context...\n"
        "✓ Created epic #42: Bug: see Created epic #99 for context\n"
    )
    assert mcp_server._parse_new_epic_number(stdout) == 42


def test_parse_new_epic_number_returns_none_without_success_line():
    stdout = "Error: epic creation failed; see Created epic #99 in audit\n"
    assert mcp_server._parse_new_epic_number(stdout) is None


# -----------------------------------------------------------------------------
# _default_venv_dir empty ZH_MCP_VENV — round-4 #10.
# An empty / whitespace value must NOT silently fall through to XDG
# without diagnostic. The fall-through is preserved (so CI configs
# that clear inherited env vars still work) but a warning is emitted.
# -----------------------------------------------------------------------------


def test_default_venv_dir_warns_on_empty_zh_mcp_venv(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ZH_MCP_VENV", "")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = mcp_server._default_venv_dir()
    # Falls through to XDG default.
    assert result == (tmp_path / "zh" / "venv").resolve()
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
    # Parent already exists as a directory — proceed silently.
    parent = tmp_path / "existing"
    parent.mkdir()
    venv_dir = parent / "venv"
    mcp_server._ensure_safe_parent(venv_dir)  # no raise


def test_ensure_safe_parent_creates_one_level(tmp_path):
    # Grandparent (tmp_path) exists, parent doesn't — create ONE level.
    venv_dir = tmp_path / "zh" / "venv"
    mcp_server._ensure_safe_parent(venv_dir)
    assert (tmp_path / "zh").is_dir()


def test_ensure_safe_parent_refuses_deep_ancestor_creation(tmp_path):
    # Both grandparent AND parent are missing — refuse rather than
    # auto-create arbitrary tree (typo'd ZH_MCP_VENV protection).
    venv_dir = tmp_path / "deep" / "nested" / "tree"
    with pytest.raises(RuntimeError, match="ancestor"):
        mcp_server._ensure_safe_parent(venv_dir)
    assert not (tmp_path / "deep").exists()  # nothing was created


def test_ensure_safe_parent_refuses_regular_file_parent(tmp_path):
    # User typo'd ZH_MCP_VENV to point at a file's child path
    # (e.g. ZH_MCP_VENV=~/.bashrc/foo). `mkdir(exist_ok=True)` would
    # raise an unhelpful FileExistsError; we raise a clear message.
    parent_file = tmp_path / "stray-file"
    parent_file.write_text("not a directory")
    venv_dir = parent_file / "venv"
    with pytest.raises(RuntimeError, match="is not a directory"):
        mcp_server._ensure_safe_parent(venv_dir)


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


# -----------------------------------------------------------------------------
# v1.7.0 — _venv_build_lock (item 2)
# Smoke test: lock is acquired + released, no exceptions, lock file exists.
# Actual concurrency contention testing would need subprocess + fcntl
# coordination — out of scope for unit tests.
# -----------------------------------------------------------------------------


def test_venv_build_lock_acquires_and_releases(tmp_path):
    venv_dir = tmp_path / "zh-venv"
    lock_path = venv_dir.parent / mcp_server._BOOTSTRAP_LOCK
    with mcp_server._venv_build_lock(venv_dir):
        # Lock file exists during the with-block.
        assert lock_path.exists()
    # After release, the lock file remains (we don't clean it up — that
    # matches the documented "lockfile-as-persistent-coordinator" pattern).
    assert lock_path.exists()


def test_venv_build_lock_creates_parent_dir(tmp_path):
    # Even when the parent doesn't exist yet, the lock context creates
    # it so subsequent acquisitions don't fail.
    venv_dir = tmp_path / "new-parent" / "venv"
    assert not venv_dir.parent.exists()
    with mcp_server._venv_build_lock(venv_dir):
        assert venv_dir.parent.is_dir()
