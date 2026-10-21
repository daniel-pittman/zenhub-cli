"""MCP tools driven into the real sourced `zh` with only GraphQL stubbed.

Pins that free-text values (release references, descriptions, titles, anchors)
that look like options reach the CLI as data, and that `pipeline_delete`
refuses a partial name, end to end rather than as argv shape alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server  # noqa: E402
import test_pipeline_commands as pc  # noqa: E402
import test_release_commands as rc  # noqa: E402
from _bash_runner import run_zh_with_stubs  # noqa: E402


def _bridge(monkeypatch, stubs):
    """Replace only process spawning: the wrappers' argv runs through real `main`."""
    runs = []

    def run(args, **kwargs):
        r = run_zh_with_stubs(stubs, 'main "$@"', args=args)
        runs.append(r)
        return {"ok": r.returncode == 0, "exit_code": r.returncode,
                "stdout_plain": r.stdout, "stderr_plain": r.stderr}

    monkeypatch.setattr(mcp_server, "_run_zh", run)
    return runs


def _mutations(runs):
    return [json.loads(line[len("MUTATION="):]) for r in runs
            for line in r.stderr.splitlines() if line.startswith("MUTATION=")]


@pytest.mark.parametrize("ref", ["--description", "--json", "-t", "--no-urls"])
def test_release_edit_with_a_flag_named_reference_hits_that_release(monkeypatch, ref):
    runs = _bridge(monkeypatch, rc._stubs([rc._release("intended", ref),
                                           rc._release("other", "Spring launch")]))
    out = mcp_server.release_edit(ref, title="Spring launch")
    assert out["ok"] is True, out
    assert _mutations(runs) == [{"input": {"releaseId": "intended",
                                           "release": {"title": "Spring launch"}}}]


@pytest.mark.parametrize("ref", ["--json", "--description", "-t", "--no-urls"])
def test_release_show_with_a_flag_named_reference_displays_it(monkeypatch, ref):
    _bridge(monkeypatch, rc._stubs([rc._release("intended", ref)]))
    out = mcp_server.release_show(ref)
    assert out["ok"] is True, out
    assert out["release"]["id"] == "intended"
    assert out["release"]["title"] == ref


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_release_edit_help_shaped_values_are_sent_literally(monkeypatch, value):
    runs = _bridge(monkeypatch, rc._stubs([rc._release("rel", "Spring launch")]))
    out = mcp_server.release_edit("Spring launch", title=value, description=value)
    assert out["ok"] is True, out
    assert _mutations(runs) == [{"input": {"releaseId": "rel",
                                           "release": {"title": value, "description": value}}}]


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_release_create_help_shaped_title_is_sent_literally(monkeypatch, value):
    runs = _bridge(monkeypatch, rc._stubs([]))
    out = mcp_server.release_create(value, "2026-05-01", "2026-05-31", description=value)
    assert out["ok"] is True, out
    sent = _mutations(runs)[0]["input"]["release"]
    assert sent["title"] == value and sent["description"] == value


@pytest.mark.parametrize("sub", ["close", "reopen"])
def test_release_state_change_with_a_help_shaped_reference(monkeypatch, sub):
    state = "CLOSED" if sub == "close" else "OPEN"
    runs = _bridge(monkeypatch, rc._stubs(
        [rc._release("rel", "-h", state="OPEN" if sub == "close" else "CLOSED")]))
    out = getattr(mcp_server, f"release_{sub}")("-h")
    assert out["ok"] is True, out
    assert _mutations(runs) == [{"input": {"releaseId": "rel", "release": {"state": state}}}]


def test_cli_help_flag_still_prints_help_for_release():
    r = run_zh_with_stubs(rc._stubs(), 'main "$@"',
                          args=["release", "edit", "x", "-t", "y", "-h"])
    assert r.returncode == 0 and "USAGE" in r.stdout
    assert "MUTATION=" not in r.stderr


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_pipeline_description_help_shaped_value_is_sent(monkeypatch, value):
    runs = _bridge(monkeypatch, pc._stubs())
    created = mcp_server.pipeline_create("Review", description=value)
    assert created["ok"] is True, created
    renamed = mcp_server.pipeline_rename("Doing", "Working", description=value)
    assert renamed["ok"] is True, renamed
    sent = _mutations(runs)
    assert sent[0]["input"]["description"] == value
    assert sent[1]["input"]["description"] == value


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_pipeline_anchor_help_shaped_value_is_not_help(monkeypatch, value):
    runs = _bridge(monkeypatch, pc._stubs())
    out = mcp_server.pipeline_create("Review", after=value)
    assert out["ok"] is False
    assert "Pipeline not found" in out["stderr"]
    assert not _mutations(runs)


@pytest.mark.parametrize("name", ["one", "Do", "review"])
def test_pipeline_delete_refuses_partial_name(monkeypatch, name):
    runs = _bridge(monkeypatch, pc._stubs())
    out = mcp_server.pipeline_delete(name, confirm=True)
    assert out["ok"] is False
    assert "No pipeline named exactly" in out["stderr"] or "not found" in out["stderr"]
    assert not _mutations(runs)


def test_pipeline_delete_exact_name_proceeds(monkeypatch):
    runs = _bridge(monkeypatch, pc._stubs())
    out = mcp_server.pipeline_delete("done", confirm=True)
    assert out["ok"] is True, out
    assert _mutations(runs)[0]["input"]["pipelineId"] == "p3"
