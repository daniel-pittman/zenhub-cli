"""Regression tests for the assign/unassign destructive-default bug (#80, #81).

The bug: `unassign` with no/empty target removed EVERY assignee. Reachable
because the MCP layer's `user` defaulted to "" (and a misnamed kwarg was
silently dropped), so a missing target silently un-assigned teammates on a
shared issue. These pin the fix end-to-end:

  - bash `cmd_unassign`: removes ONLY the named user(s); clearing everyone now
    requires an explicit `--all`; a bare target-less call hard-errors instead of
    clearing all.
  - bash `cmd_assign` / `cmd_unassign`: accept multiple users (symmetric).
  - MCP `assign` / `unassign`: canonical `assignees` list (+ `user` alias);
    `unassign` never clears all without `clear_all=True`, and calling with no
    target returns an error WITHOUT invoking zh (no destructive fallback).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server  # noqa: E402
from _bash_runner import run_zh_with_stubs  # noqa: E402

# --- shared bash stubs ------------------------------------------------------
# Issue #882 has two assignees (daniel-pittman, alinavalshchuk). The zh_graphql
# stub echoes the assignee IDs sent to the add/remove mutation as a marker so a
# test can assert exactly which users were touched.
_UNASSIGN_STUBS = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_repo_id() { printf 'repo-gid'; }
    get_workspace_id() { printf 'ws-gid'; }
    zh_graphql() {
        local q="$1" v="$2"
        if [[ "$q" == *removeAssigneesFromIssues* ]]; then
            echo "REMOVE_IDS=$(printf '%s' "$v" | jq -c '.input.assigneeIds | sort')" >&2
            printf '{"data":{"removeAssigneesFromIssues":{"successCount":1,"githubErrors":null}}}'
        else
            printf '%s' '{"data":{"issueByInfo":{"id":"iid","title":"T","assignees":{"nodes":[{"id":"uid-daniel","login":"daniel-pittman"},{"id":"uid-alina","login":"alinavalshchuk"}]}}}}'
        fi
    }
"""

_ASSIGN_STUBS = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_repo_id() { printf 'repo-gid'; }
    get_workspace_id() { printf 'ws-gid'; }
    zh_graphql() {
        local q="$1" v="$2"
        if [[ "$q" == *addAssigneesToIssues* ]]; then
            echo "ADD_IDS=$(printf '%s' "$v" | jq -c '.input.assigneeIds | sort')" >&2
            printf '{"data":{"addAssigneesToIssues":{"successCount":2,"githubErrors":null}}}'
        elif [[ "$q" == *workspace* ]]; then
            printf '%s' '{"data":{"workspace":{"assignees":{"nodes":[{"id":"uid-alice","login":"alice"},{"id":"uid-bob","login":"bob"}]}}}}'
        else
            printf '%s' '{"data":{"issueByInfo":{"id":"iid","title":"T","assignees":{"nodes":[]}}}}'
        fi
    }
"""


# --- bash: unassign ---------------------------------------------------------

def test_unassign_named_user_removes_only_that_user() -> None:
    """#80 core: unassign one of two assignees → only that user's ID is sent to
    the remove mutation; the teammate is untouched."""
    r = run_zh_with_stubs(_UNASSIGN_STUBS, "cmd_unassign 882 daniel-pittman")
    assert r.returncode == 0, r.stderr
    assert 'REMOVE_IDS=["uid-daniel"]' in r.stderr, f"expected only daniel's ID; got {r.stderr!r}"
    assert "uid-alina" not in r.stderr, "teammate must NOT be removed"


def test_unassign_no_target_refuses_to_clear_all() -> None:
    """The destructive default is gone: no user + no --all → hard error, and the
    remove mutation is NEVER reached."""
    r = run_zh_with_stubs(_UNASSIGN_STUBS, "cmd_unassign 882")
    assert r.returncode != 0
    assert "Refusing to remove all assignees" in r.stderr
    assert "REMOVE_IDS" not in r.stderr, "must not fire the remove mutation"


def test_unassign_all_flag_clears_everyone() -> None:
    """Explicit --all removes every assignee."""
    r = run_zh_with_stubs(_UNASSIGN_STUBS, "cmd_unassign 882 --all")
    assert r.returncode == 0, r.stderr
    assert "uid-alina" in r.stderr and "uid-daniel" in r.stderr, f"--all should remove both; got {r.stderr!r}"


def test_unassign_unknown_user_errors_without_mutating() -> None:
    """A named user who isn't assigned is a hard error (not a silent no-op or a
    fall-through to clear-all)."""
    r = run_zh_with_stubs(_UNASSIGN_STUBS, "cmd_unassign 882 ghost")
    assert r.returncode != 0
    assert "Not assigned" in r.stderr
    assert "REMOVE_IDS" not in r.stderr


def test_unassign_multiple_named_users() -> None:
    """Multi-user removal: both named users' IDs sent, nobody else."""
    r = run_zh_with_stubs(_UNASSIGN_STUBS, "cmd_unassign 882 daniel-pittman alinavalshchuk")
    assert r.returncode == 0, r.stderr
    assert 'REMOVE_IDS=["uid-alina","uid-daniel"]' in r.stderr, f"got {r.stderr!r}"


# --- bash: assign -----------------------------------------------------------

def test_assign_multiple_users() -> None:
    """Symmetric multi-user assign: both users added in one mutation."""
    r = run_zh_with_stubs(_ASSIGN_STUBS, "cmd_assign 882 alice bob")
    assert r.returncode == 0, r.stderr
    assert 'ADD_IDS=["uid-alice","uid-bob"]' in r.stderr, f"got {r.stderr!r}"


def test_assign_no_user_errors() -> None:
    r = run_zh_with_stubs(_ASSIGN_STUBS, "cmd_assign 882")
    assert r.returncode != 0
    assert "Usage: zh assign" in r.stderr


# --- MCP forwarding + guards ------------------------------------------------

def _capture(monkeypatch):
    calls = []

    def fake_run_zh(args, **kwargs):
        calls.append(list(args))
        return {"ok": True, "stdout_plain": "ok", "stderr_plain": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    return calls


def test_mcp_assign_assignees_list(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.assign(number=42, assignees=["alice", "bob"])
    assert calls == [["assign", "42", "alice", "bob"]]
    assert out["ok"] and out["assignees"] == ["alice", "bob"]


def test_mcp_assign_user_alias(monkeypatch):
    calls = _capture(monkeypatch)
    mcp_server.assign(number=42, user="alice")
    assert calls == [["assign", "42", "alice"]]


def test_mcp_assign_empty_errors_without_calling_zh(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.assign(number=42)
    assert out["ok"] is False
    assert calls == [], "must not invoke zh with no assignee"


def test_mcp_unassign_assignees_list(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.unassign(number=42, assignees=["alice"])
    assert calls == [["unassign", "42", "alice"]]
    assert out["cleared_all"] is False


def test_mcp_unassign_clear_all(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.unassign(number=42, clear_all=True)
    assert calls == [["unassign", "42", "--all"]]
    assert out["cleared_all"] is True


def test_mcp_unassign_no_target_is_safe(monkeypatch):
    """THE regression: unassign with no target must error and NOT invoke zh —
    never a silent clear-all (this is what bit the real shared issue)."""
    calls = _capture(monkeypatch)
    out = mcp_server.unassign(number=42)
    assert out["ok"] is False
    assert "Refusing to remove all assignees" in out["stderr"]
    assert calls == [], "must not invoke zh (no destructive fallback)"


def test_mcp_unassign_both_assignees_and_clear_all_errors(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.unassign(number=42, assignees=["alice"], clear_all=True)
    assert out["ok"] is False
    assert calls == []


def test_mcp_unassign_misnamed_kwarg_does_not_clear_all(monkeypatch):
    """The original foot-gun: a wrong kwarg name must not fall through to
    clear-all. With `assignees` as the canonical param and the no-target guard,
    even if only `user`-style intent is lost, nothing destructive runs."""
    calls = _capture(monkeypatch)
    # Simulate the dropped-arg situation: caller meant to name a user but the
    # value didn't land in assignees/user. The guard must catch it.
    out = mcp_server.unassign(number=42, assignees=[])
    assert out["ok"] is False
    assert calls == []
