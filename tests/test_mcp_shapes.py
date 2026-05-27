"""Tests for MCP tool early-return shape consistency.

The MCP sprint tools have empty-input guards (empty `sprint_name` or
empty `issue_numbers`) that short-circuit before any network call.
Pre-fix those guards returned `{ok: False, stderr: ...}` with NO
other documented keys — a strict MCP caller doing
`result["sprint_name"]` after the guard fired would KeyError.

After the fix (review #9), every guard return matches the full
documented key set for that tool, so `.get("...")` and `[...]` access
both work uniformly across success and error paths.

These tests exercise only the guards. They don't make network calls.
"""

from __future__ import annotations

# The mcp_server module's import path needs the repo root on sys.path;
# conftest.py handles that for the rest of the suite.
import mcp_server


# Documented key sets for each tool's return dict.
SPRINT_SHOW_KEYS = {
    "ok", "sprint_id", "sprint_name", "state", "start_at", "end_at",
    "completed_points", "total_points", "closed_issues_count",
    "description", "issue_count", "issues", "pagination_warning",
    "stderr",
}

SPRINT_ADD_KEYS = {
    "ok", "sprint_id", "sprint_name", "outcome",
    "success_count", "failed_count", "succeeded", "failed", "stderr",
}

SPRINT_REMOVE_KEYS = {
    "ok", "sprint_id", "sprint_name", "outcome",
    "success_count", "failed_count", "succeeded", "failed",
    "inspected_full", "pagination_warning", "response_anomaly",
    "stderr",
}


def _has_keys(d: dict, expected: set[str]) -> bool:
    """Every expected key is present in d (allow extras)."""
    missing = expected - set(d.keys())
    assert not missing, f"missing keys: {sorted(missing)}"
    return True


# ---- sprint_show --------------------------------------------------------

def test_sprint_show_empty_name_returns_full_shape():
    r = mcp_server.sprint_show("")
    assert r["ok"] is False
    assert "non-empty" in r["stderr"].lower()
    _has_keys(r, SPRINT_SHOW_KEYS)


def test_sprint_show_whitespace_name_returns_full_shape():
    r = mcp_server.sprint_show("   ")
    assert r["ok"] is False
    _has_keys(r, SPRINT_SHOW_KEYS)


# ---- sprint_add_issues -------------------------------------------------

def test_sprint_add_empty_issue_numbers_returns_full_shape():
    r = mcp_server.sprint_add_issues("Sprint 7", [])
    assert r["ok"] is False
    assert "issue_numbers" in r["stderr"]
    _has_keys(r, SPRINT_ADD_KEYS)


def test_sprint_add_empty_sprint_name_returns_full_shape():
    r = mcp_server.sprint_add_issues("", [42])
    assert r["ok"] is False
    assert "sprint_name" in r["stderr"]
    _has_keys(r, SPRINT_ADD_KEYS)


def test_sprint_add_whitespace_sprint_name_returns_full_shape():
    r = mcp_server.sprint_add_issues("   ", [42])
    assert r["ok"] is False
    _has_keys(r, SPRINT_ADD_KEYS)


# ---- sprint_remove_issues ----------------------------------------------

def test_sprint_remove_empty_issue_numbers_returns_full_shape():
    r = mcp_server.sprint_remove_issues("Sprint 7", [])
    assert r["ok"] is False
    assert "issue_numbers" in r["stderr"]
    _has_keys(r, SPRINT_REMOVE_KEYS)


def test_sprint_remove_empty_sprint_name_returns_full_shape():
    r = mcp_server.sprint_remove_issues("", [42])
    assert r["ok"] is False
    assert "sprint_name" in r["stderr"]
    _has_keys(r, SPRINT_REMOVE_KEYS)


def test_sprint_remove_full_shape_includes_new_fields():
    """The post-Bucket A fields (inspected_full, pagination_warning,
    response_anomaly) must show up in the early-return shape too.
    """
    r = mcp_server.sprint_remove_issues("Sprint 7", [])
    assert "inspected_full" in r
    assert "pagination_warning" in r
    assert "response_anomaly" in r


# ---- subissue_add_children / subissue_remove_children (round-3 #4) -------

SUBISSUE_MUTATION_KEYS = {
    "ok", "parent_number", "outcome",
    "success_count", "failed_count", "succeeded", "failed",
    "github_errors", "partial_success_warning", "stderr",
}


def test_subissue_add_children_empty_returns_full_shape():
    """Round-3 #4: bare `{ok, stderr}` was the bug; full documented
    key set is the SPEC."""
    r = mcp_server.subissue_add_children(42, [])
    assert r["ok"] is False
    assert "child_numbers" in r["stderr"]
    _has_keys(r, SUBISSUE_MUTATION_KEYS)


def test_subissue_remove_children_empty_returns_full_shape():
    """Round-3 #4 — same SPEC on the remove side."""
    r = mcp_server.subissue_remove_children(42, [])
    assert r["ok"] is False
    assert "child_numbers" in r["stderr"]
    _has_keys(r, SUBISSUE_MUTATION_KEYS)


def test_run_zh_strips_ansi_from_stderr_plain(monkeypatch):
    """Round-6 #15 SPEC pin: `_run_zh` returns both `stdout_plain`
    AND `stderr_plain` with ANSI escape codes stripped. Pre-fix,
    only stdout got the strip — MCP callers surfacing tool errors
    saw raw `\\x1b[...m` codes embedded in error messages.
    """
    import subprocess

    class _FakeResult:
        def __init__(self):
            self.returncode = 1
            self.stdout = "\x1b[31mfoo\x1b[0m"
            self.stderr = "\x1b[31mError: not found\x1b[0m"

    def fake_run(*args, **kwargs):
        return _FakeResult()

    # Patch ZH_BIN.exists so _run_zh proceeds past the early-return
    # binary-missing branch.
    import pathlib
    monkeypatch.setattr(
        pathlib.Path, "exists", lambda self: True
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = mcp_server._run_zh(["test"])
    # Original streams retain escape codes
    assert "\x1b[31m" in result["stdout"]
    assert "\x1b[31m" in result["stderr"]
    # _plain variants strip them
    assert result["stdout_plain"] == "foo"
    assert result["stderr_plain"] == "Error: not found", (
        f"stderr_plain should be ANSI-free; got {result['stderr_plain']!r}"
    )


def test_run_zh_binary_missing_returns_stderr_plain():
    """Round-6 #15: even the early-return path when the binary is
    missing must include `stderr_plain` for shape consistency
    (callers that always read .get('stderr_plain', '') don't
    KeyError on this path).
    """
    import pathlib
    from unittest.mock import patch
    with patch.object(pathlib.Path, "exists", return_value=False):
        result = mcp_server._run_zh(["test"])
    assert "stderr_plain" in result
    # The early-return path has no ANSI in its synthesized message,
    # but stderr_plain must still be a string.
    assert isinstance(result["stderr_plain"], str)


def test_run_zh_timeout_stderr_includes_captured_diagnostic(monkeypatch):
    """Round-7 #5 SPEC pin: the timeout branch's `stderr` and
    `stderr_plain` describe the same subprocess state with vs
    without ANSI escapes. Pre-fix `stderr` was the synthetic
    timeout message only — callers reading it lost any diagnostic
    the subprocess had emitted before timing out.

    SPEC: both fields contain the captured diagnostic (when
    present) AND the synthetic timeout suffix.
    """
    import subprocess

    def fake_run(*args, **kwargs):
        # TimeoutExpired's stdout/stderr are set as attributes after
        # __init__, not kwargs to it.
        exc = subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 60),
        )
        exc.stdout = "partial stdout output"
        exc.stderr = "\x1b[31mfatal: about to fail\x1b[0m"
        raise exc

    import pathlib
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = mcp_server._run_zh(["test"], timeout=5.0)
    # stderr retains the captured diagnostic (with ANSI)
    assert "fatal: about to fail" in result["stderr"], (
        f"Round-7 #5: stderr must include the captured diagnostic; "
        f"got {result['stderr']!r}"
    )
    assert "\x1b[31m" in result["stderr"]
    # stderr_plain has the same content with ANSI stripped
    assert "fatal: about to fail" in result["stderr_plain"]
    assert "\x1b[31m" not in result["stderr_plain"]
    # Both fields include the synthetic timeout suffix
    assert "timed out after" in result["stderr"]
    assert "timed out after" in result["stderr_plain"]


def test_run_zh_timeout_no_captured_stderr_keeps_synthetic_only(monkeypatch):
    """Round-7 #5 — when the timeout has no captured stderr, the
    synthetic message stands alone (no leading newline)."""
    import subprocess

    def fake_run(*args, **kwargs):
        exc = subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 60),
        )
        # exc.stdout / exc.stderr default to None when unset
        raise exc

    import pathlib
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = mcp_server._run_zh(["test"], timeout=5.0)
    # No spurious leading newline when there's no captured diagnostic
    assert not result["stderr"].startswith("\n"), (
        f"unexpected leading newline: {result['stderr']!r}"
    )
    assert "timed out after" in result["stderr"]
    assert result["stderr"] == result["stderr_plain"]  # no ANSI to strip
