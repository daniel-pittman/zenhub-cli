"""MCP parity with `zh create --body-file` and `zh comment --file` (#140).

`create_issue` takes `body_file` and `comment` takes `message_file`, each
mutually exclusive with its inline field. The file's text must reach `zh`
byte for byte (backticks, non-ASCII, trailing newlines), and every refusal
must happen before `zh` is called, so a bad path never sends anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server  # noqa: E402

BODY = "## Debt\n\n- [ ] `zh release show -- \"-dash\"` refuses\n- café ✓ ``nested `ticks` ``\n"

CREATED = ('{"number": 7, "url": "https://github.com/o/r/issues/7", '
           '"type": "Task", "pipeline": "New Issues", "parent": null, '
           '"estimate": null, "estimate_requested": null, '
           '"priority": null, "priority_requested": null}')


def _capture(monkeypatch, ok: bool = True, stdout: str = CREATED) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run_zh(args, **kwargs):  # noqa: ARG001
        seen.append(args)
        return {"ok": ok, "exit_code": 0 if ok else 1,
                "stdout_plain": stdout, "stderr_plain": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    return seen


def _arg_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# ---- create_issue ---------------------------------------------------------

def test_create_issue_reads_body_file_verbatim(monkeypatch, tmp_path) -> None:
    f = tmp_path / "body.md"
    f.write_bytes(BODY.encode("utf-8"))
    seen = _capture(monkeypatch)

    out = mcp_server.create_issue("Title", body_file=str(f),
                                  skip_duplicate_check=True)

    assert out["ok"] is True and out["number"] == 7
    assert _arg_after(seen[0], "-b") == BODY


def test_create_issue_inline_body_unchanged(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", "inline body",
                                  skip_duplicate_check=True)
    assert out["ok"] is True
    assert _arg_after(seen[0], "-b") == "inline body"


def test_create_issue_refuses_body_and_body_file(monkeypatch, tmp_path) -> None:
    f = tmp_path / "body.md"
    f.write_text("from file", encoding="utf-8")
    seen = _capture(monkeypatch)

    out = mcp_server.create_issue("Title", "inline", body_file=str(f),
                                  skip_duplicate_check=True)

    assert out["ok"] is False
    assert "not both" in out["stderr"]
    assert out["duplicate_check"]["recommendation"] == "skipped"
    assert seen == [], "a refusal must not call zh"


def test_create_issue_refuses_missing_file(monkeypatch, tmp_path) -> None:
    seen = _capture(monkeypatch)
    missing = tmp_path / "nope.md"
    out = mcp_server.create_issue("Title", body_file=str(missing),
                                  skip_duplicate_check=True)
    assert out["ok"] is False
    assert "could not be read" in out["stderr"]
    assert out["number"] is None
    assert seen == []


def test_create_issue_refuses_directory(monkeypatch, tmp_path) -> None:
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", body_file=str(tmp_path),
                                  skip_duplicate_check=True)
    assert out["ok"] is False
    assert "could not be read" in out["stderr"]
    assert seen == []


def test_create_issue_refuses_relative_path(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", body_file="body.md",
                                  skip_duplicate_check=True)
    assert out["ok"] is False
    assert "absolute path" in out["stderr"]
    assert seen == []


def test_create_issue_refuses_non_utf8(monkeypatch, tmp_path) -> None:
    f = tmp_path / "latin1.md"
    f.write_bytes("caf\xe9".encode("latin-1"))
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", body_file=str(f),
                                  skip_duplicate_check=True)
    assert out["ok"] is False
    assert "not valid UTF-8" in out["stderr"]
    assert seen == []


def test_create_issue_refuses_empty_file(monkeypatch, tmp_path) -> None:
    f = tmp_path / "empty.md"
    f.write_text("  \n\n", encoding="utf-8")
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", body_file=str(f),
                                  skip_duplicate_check=True)
    assert out["ok"] is False
    assert out["stderr"] == "body must be non-empty"
    assert seen == []


def test_create_issue_still_requires_a_body(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", skip_duplicate_check=True)
    assert out["ok"] is False
    assert out["stderr"] == "body must be non-empty"
    assert seen == []


def test_duplicate_check_sees_the_file_text(monkeypatch, tmp_path) -> None:
    f = tmp_path / "body.md"
    f.write_text(BODY, encoding="utf-8")
    _capture(monkeypatch)
    checked: list[str] = []

    import similarity

    def fake_check(title, body, repo, **kwargs):  # noqa: ARG001
        checked.append(body)
        return {"ok": True, "recommendation": "create", "matches": []}

    monkeypatch.setattr(mcp_server, "_similarity_repo", lambda _p: ("o/r", None))
    monkeypatch.setattr(similarity, "check_duplicate", fake_check)

    out = mcp_server.create_issue("Title", body_file=str(f))

    assert out["ok"] is True
    assert checked == [BODY]


# ---- comment ----------------------------------------------------------------

def test_comment_reads_message_file_verbatim(monkeypatch, tmp_path) -> None:
    f = tmp_path / "comment.md"
    f.write_bytes(BODY.encode("utf-8"))
    seen = _capture(monkeypatch, stdout="")

    out = mcp_server.comment(42, message_file=str(f))

    assert out["ok"] is True
    assert seen[0][:2] == ["comment", "42"]
    assert _arg_after(seen[0], "-m") == BODY


def test_comment_refuses_message_and_file(monkeypatch, tmp_path) -> None:
    f = tmp_path / "comment.md"
    f.write_text("from file", encoding="utf-8")
    seen = _capture(monkeypatch, stdout="")
    out = mcp_server.comment(42, "inline", message_file=str(f))
    assert out["ok"] is False
    assert "not both" in out["stderr"]
    assert set(out) == {"ok", "partial_applied", "number", "raw", "stderr"}
    assert seen == []


def test_comment_refuses_missing_and_empty_file(monkeypatch, tmp_path) -> None:
    seen = _capture(monkeypatch, stdout="")
    out = mcp_server.comment(42, message_file=str(tmp_path / "nope.md"))
    assert out["ok"] is False and "could not be read" in out["stderr"]

    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    out = mcp_server.comment(42, message_file=str(empty))
    assert out["ok"] is False and out["stderr"] == "message must be non-empty"
    assert seen == []
