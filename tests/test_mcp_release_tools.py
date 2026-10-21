"""Tests for the release_* MCP tools.

`_run_zh` is stubbed, so these pin the argv each tool builds and how it
shapes zh's stdout/exit status; the CLI behavior itself is covered by
test_release_commands.py.
"""

from __future__ import annotations

import json

import mcp_server


def _capture(monkeypatch, stdout="", ok=True, stderr=""):
    calls = []

    def fake_run_zh(args, **kwargs):
        calls.append(list(args))
        return {"ok": ok, "stdout_plain": stdout, "stderr_plain": stderr}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    return calls


_LIST = json.dumps({
    "workspace": "WS",
    "releases": [{"id": "r1", "title": "Spring", "state": "OPEN"}],
    "pagination_bailed": "",
})


def test_release_list_open_by_default(monkeypatch):
    calls = _capture(monkeypatch, stdout=_LIST)
    out = mcp_server.release_list()
    assert calls == [["releases", "--json"]]
    assert out["ok"] is True and out["incomplete"] is False
    assert out["workspace"] == "WS"
    assert out["releases"][0]["title"] == "Spring"


def test_release_list_include_closed_adds_all(monkeypatch):
    calls = _capture(monkeypatch, stdout=_LIST)
    mcp_server.release_list(include_closed=True)
    assert calls == [["releases", "--json", "--all"]]


def test_release_list_incomplete_listing_is_ok_but_flagged(monkeypatch):
    """The CLI exits 2 with the JSON on stdout when pagination bails."""
    payload = json.dumps({"workspace": "WS", "releases": [],
                          "pagination_bailed": "stuck_cursor"})
    _capture(monkeypatch, stdout=payload, ok=False)
    out = mcp_server.release_list()
    assert out["ok"] is True
    assert out["incomplete"] is True
    assert out["pagination_bailed"] == "stuck_cursor"


def test_release_list_failure_without_payload(monkeypatch):
    _capture(monkeypatch, stdout="", ok=False, stderr="Error: boom")
    out = mcp_server.release_list()
    assert out["ok"] is False
    assert out["releases"] == []
    assert "boom" in out["stderr"]


def test_release_show_splits_release_and_issues(monkeypatch):
    payload = json.dumps({"id": "r1", "title": "Spring", "state": "OPEN",
                          "issues": [{"number": 7}], "pagination_bailed": ""})
    calls = _capture(monkeypatch, stdout=payload)
    out = mcp_server.release_show("Spring")
    assert calls == [["release", "show", "Spring", "--json"]]
    assert out["ok"] is True
    assert out["release"]["id"] == "r1"
    assert "issues" not in out["release"]
    assert out["issues"] == [{"number": 7}]
    assert out["incomplete"] is False


def test_release_show_flags_incomplete_issue_list(monkeypatch):
    payload = json.dumps({"id": "r1", "issues": [], "pagination_bailed": "iteration_cap"})
    _capture(monkeypatch, stdout=payload, ok=False)
    out = mcp_server.release_show("r1")
    assert out["ok"] is True and out["incomplete"] is True


def test_release_show_not_found(monkeypatch):
    _capture(monkeypatch, stdout="", ok=False, stderr="Release 'x' not found.")
    out = mcp_server.release_show("x")
    assert out["ok"] is False and out["release"] is None
    assert "not found" in out["stderr"]


def test_release_show_dash_title_goes_through_double_dash(monkeypatch):
    calls = _capture(monkeypatch, stdout="{}")
    mcp_server.release_show("-odd")
    assert calls == [["--", "release", "show", "-odd", "--json"]]


def test_release_tools_reject_empty_reference_without_running_zh(monkeypatch):
    calls = _capture(monkeypatch)
    for out in (mcp_server.release_show("  "), mcp_server.release_edit("", title="x"),
                mcp_server.release_close(""), mcp_server.release_reopen("")):
        assert out["ok"] is False
        assert "non-empty" in out["stderr"]
    assert calls == []


def test_release_create_forwards_everything(monkeypatch):
    created = json.dumps({"id": "r-new", "title": "Summer"})
    calls = _capture(monkeypatch, stdout=created)
    out = mcp_server.release_create("Summer", "2026-05-01", "2026-05-31",
                                    description="Notes")
    assert calls == [["release", "create", "Summer", "--start", "2026-05-01",
                      "--end", "2026-05-31", "--json", "--description", "Notes"]]
    assert out["ok"] is True and out["id"] == "r-new"


def test_release_create_omits_empty_description(monkeypatch):
    calls = _capture(monkeypatch, stdout='{"id": "r"}')
    mcp_server.release_create("S", "2026-05-01", "2026-05-01")
    assert "--description" not in calls[0]


def test_release_create_validates_before_running(monkeypatch):
    calls = _capture(monkeypatch)
    assert mcp_server.release_create("", "2026-05-01", "2026-05-31")["ok"] is False
    assert mcp_server.release_create("S", "", "2026-05-31")["ok"] is False
    assert calls == []


def test_release_create_failure(monkeypatch):
    _capture(monkeypatch, stdout="", ok=False, stderr="Invalid --start date")
    out = mcp_server.release_create("S", "2026-02-31", "2026-03-01")
    assert out["ok"] is False and out["id"] is None
    assert "Invalid --start date" in out["stderr"]


def test_release_edit_sends_only_given_fields(monkeypatch):
    calls = _capture(monkeypatch, stdout="Updated release: X")
    out = mcp_server.release_edit("Spring", end="2026-04-07")
    assert calls == [["release", "edit", "Spring", "--end", "2026-04-07"]]
    assert out["ok"] is True


def test_release_edit_empty_description_clears_it(monkeypatch):
    calls = _capture(monkeypatch)
    mcp_server.release_edit("Spring", description="")
    assert calls == [["release", "edit", "Spring", "--description", ""]]


def test_release_edit_requires_a_change(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.release_edit("Spring")
    assert out["ok"] is False and "at least one" in out["stderr"]
    assert calls == []


def test_release_close_and_reopen(monkeypatch):
    calls = _capture(monkeypatch, stdout="Release CLOSED: Spring")
    assert mcp_server.release_close("Spring")["ok"] is True
    assert mcp_server.release_reopen("Spring")["ok"] is True
    assert calls == [["release", "close", "Spring"], ["release", "reopen", "Spring"]]


def test_release_close_surfaces_cli_refusal(monkeypatch):
    _capture(monkeypatch, ok=False, stderr="Pass the release id instead")
    out = mcp_server.release_close("Spring")
    assert out["ok"] is False and "release id" in out["stderr"]


def test_no_release_delete_tool():
    assert not hasattr(mcp_server, "release_delete")
