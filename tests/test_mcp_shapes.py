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
