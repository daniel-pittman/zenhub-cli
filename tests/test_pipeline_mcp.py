"""MCP pipeline tools preserve CLI argv, scope, and write result shapes."""

import json

import pytest

import mcp_server


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def run(args, cwd=None):
        seen.append((args, cwd))
        return {"ok": True, "stdout_plain": json.dumps({"id": "p-new", "name": "New",
                 "pipelines": [{"id": "p-new", "name": "New", "changed": True}]}),
                "stderr": "", "stderr_plain": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", run)
    return seen


def test_create_forwards_scope_description_and_zero_position(calls):
    r = mcp_server.pipeline_create("--new", position=0, description="",
                                   repo_path="/tmp/repo", workspace="Team")
    assert calls == [(["-w", "Team", "pipeline", "create", "--json", "--position", "0",
                      "-d", "", "--", "--new"], "/tmp/repo")]
    assert r["ok"] and r["id"] == "p-new"
    assert r["pipelines"] == [{"id": "p-new", "name": "New", "changed": True}]
    assert r["pipeline"] == "--new" and r["partial_applied"] is False
    assert r["stderr"] == ""


def test_create_defaults_append_and_resolve_default_cwd(calls, monkeypatch):
    monkeypatch.setenv("ZH_DEFAULT_REPO_PATH", "/tmp/default")
    r = mcp_server.pipeline_create("New")
    assert calls == [(["pipeline", "create", "--json", "--", "New"], "/tmp/default")]
    assert r["ok"]


@pytest.mark.parametrize("tool", ["pipeline_create", "pipeline_move"])
@pytest.mark.parametrize("kwargs,flags", [({"after": "A"}, ["--after", "A"]),
                                        ({"before": "B"}, ["--before", "B"]),
                                        ({"position": 0}, ["--position", "0"])])
def test_placement_flags_forwarded(tool, kwargs, flags, calls):
    getattr(mcp_server, tool)("Target", repo_path="/tmp/repo", **kwargs)
    prefix = ["pipeline", "create", "--json"] if tool == "pipeline_create" else ["pipeline", "move"]
    assert calls == [(prefix + flags + ["--", "Target"], "/tmp/repo")]


def test_move_leaves_placement_validation_to_cli(calls):
    mcp_server.pipeline_move("Target", after="A", before="B", position=0, workspace="Team")
    assert calls[0][0] == ["-w", "Team", "pipeline", "move", "--after", "A", "--before", "B",
                          "--position", "0", "--", "Target"]


@pytest.mark.parametrize("description,flags", [(None, []), ("Notes", ["-d", "Notes"]), ("", ["-d", ""])])
def test_rename_forwards_optional_description_and_escaped_names(description, flags, calls):
    r = mcp_server.pipeline_rename("--old", "--new", description=description, repo_path="/tmp/repo", workspace="Team")
    assert calls == [(["-w", "Team", "pipeline", "rename", *flags, "--", "--old", "--new"], "/tmp/repo")]
    assert set(r) == {"ok", "partial_applied", "pipeline", "raw", "stderr"}


@pytest.mark.parametrize("confirm", [False, None, "yes", 1])
def test_delete_requires_explicit_true_and_never_runs_cli(confirm, calls):
    r = mcp_server.pipeline_delete("Target", confirm=confirm)
    assert calls == []
    assert r == {"ok": False, "partial_applied": False, "pipeline": "Target",
                 "confirmation_required": True, "raw": "",
                 "stderr": "Propose the pipeline deletion first, then pass confirm=True after approval."}


def test_delete_default_is_propose_first(calls):
    r = mcp_server.pipeline_delete("Target")
    assert r["confirmation_required"] and not calls
    assert "Propose first" in mcp_server.pipeline_delete.__doc__


def test_delete_confirm_passes_yes_and_context(calls):
    r = mcp_server.pipeline_delete("--target", confirm=True, repo_path="/tmp/repo", workspace="Team")
    assert calls == [(["-w", "Team", "pipeline", "delete", "-y", "--", "--target"], "/tmp/repo")]
    assert r["ok"] and r["confirmation_required"] is False


@pytest.mark.parametrize("tool,kwargs", [
    ("pipeline_create", {}), ("pipeline_move", {"position": 0}),
    ("pipeline_rename", {"new_name": "New"}), ("pipeline_delete", {"confirm": True}),
])
def test_cli_failures_preserve_documented_shape(tool, kwargs, monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_zh", lambda *a, **kw: {
        "ok": False, "stdout_plain": "", "stderr": "Error: refused", "stderr_plain": "Error: refused"})
    r = getattr(mcp_server, tool)("Target", **kwargs)
    assert r["ok"] is False and r["pipeline"] == "Target"
    assert r["stderr"] == "Error: refused" and r["raw"] == ""
    assert r["partial_applied"] is False
    if tool == "pipeline_create":
        assert r["id"] is None and r["pipelines"] == []


def test_read_pipeline_escapes_verb_names(calls):
    mcp_server.pipeline("create", repo_path="/tmp/repo")
    assert calls == [(["pipeline", "--", "create"], "/tmp/repo")]
