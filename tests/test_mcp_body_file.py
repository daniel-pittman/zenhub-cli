"""MCP parity with `zh create --body-file` and `zh comment --file` (#140).

`create_issue` takes `body_file` and `comment` takes `message_file`, each
mutually exclusive with its inline field. The text, inline or from a file,
reaches `zh` on stdin exactly as given (backticks, non-ASCII, a leading
`--help`) and never as an argument, and every refusal happens before `zh` is
called, so a bad path never sends anything.
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
    stdins.clear()

    def fake_run_zh(args, **kwargs):
        seen.append(args)
        stdins.append(kwargs.get("stdin"))
        return {"ok": ok, "exit_code": 0 if ok else 1,
                "stdout_plain": stdout, "stderr_plain": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    return seen


stdins: list[str | None] = []


def _assert_sent_on_stdin(args: list[str], text: str) -> None:
    assert "--stdin" in args
    assert "-b" not in args and "-m" not in args
    assert text not in args, "the text must never travel as an argument"
    assert stdins[-1] == text


# ---- create_issue ---------------------------------------------------------

def test_create_issue_reads_body_file_verbatim(monkeypatch, tmp_path) -> None:
    f = tmp_path / "body.md"
    f.write_bytes(BODY.encode("utf-8"))
    seen = _capture(monkeypatch)

    out = mcp_server.create_issue("Title", body_file=str(f),
                                  skip_duplicate_check=True)

    assert out["ok"] is True and out["number"] == 7
    _assert_sent_on_stdin(seen[0], BODY)


def test_create_issue_inline_body_unchanged(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", "inline body",
                                  skip_duplicate_check=True)
    assert out["ok"] is True
    _assert_sent_on_stdin(seen[0], "inline body")


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
    _assert_sent_on_stdin(seen[0], BODY)


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


# ---- non-regular files and GitHub's size limit --------------------------------

def test_fifo_is_refused_without_blocking(monkeypatch, tmp_path) -> None:
    """The check runs on a descriptor opened non-blocking, so a FIFO with no
    writer is refused at once. Without O_NONBLOCK the open would block; the
    alarm turns that hang into a failure."""
    import os
    import signal

    def _hung(signum, frame):  # noqa: ARG001
        raise TimeoutError("opening the FIFO blocked")

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    seen = _capture(monkeypatch)
    old = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(5)
    try:
        out = mcp_server.create_issue("Title", body_file=str(fifo),
                                      skip_duplicate_check=True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert out["ok"] is False
    assert "not a regular file" in out["stderr"]
    assert seen == []


def test_body_at_the_limit_is_sent(monkeypatch, tmp_path) -> None:
    limit = mcp_server._GITHUB_BODY_MAX_CHARS
    f = tmp_path / "max.md"
    f.write_text("é" * limit, encoding="utf-8")
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", body_file=str(f),
                                  skip_duplicate_check=True)
    assert out["ok"] is True
    assert len(stdins[-1]) == limit


def test_body_over_the_limit_is_refused(monkeypatch, tmp_path) -> None:
    limit = mcp_server._GITHUB_BODY_MAX_CHARS
    seen = _capture(monkeypatch)
    for name, text in (("chars.md", "a" * (limit + 1)),
                       ("bytes.md", "✓" * (limit * 2))):
        f = tmp_path / name
        f.write_text(text, encoding="utf-8")
        out = mcp_server.comment(42, message_file=str(f))
        assert out["ok"] is False, name
        assert "character limit" in out["stderr"], (name, out["stderr"])
    assert seen == []


# ---- review findings: flag-shaped text, odd paths, NUL bytes, swapped FIFO -----

def test_flag_shaped_text_is_data(monkeypatch, tmp_path) -> None:
    f = tmp_path / "help.md"
    f.write_text("--help", encoding="utf-8")
    seen = _capture(monkeypatch, stdout="")
    out = mcp_server.comment(42, message_file=str(f))
    assert out["ok"] is True
    _assert_sent_on_stdin(seen[0], "--help")

    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", "--help", skip_duplicate_check=True)
    assert out["ok"] is True
    _assert_sent_on_stdin(seen[0], "--help")


def test_unusable_paths_are_refused_not_raised(monkeypatch) -> None:
    seen = _capture(monkeypatch, stdout="")
    for path in ("/tmp/" + "x" * 300, "~no_such_user_zz/body.md"):
        out = mcp_server.comment(42, message_file=path)
        assert out["ok"] is False, path
        assert "could not be read" in out["stderr"], (path, out["stderr"])
    assert seen == []


def test_nul_bytes_are_refused(monkeypatch, tmp_path) -> None:
    f = tmp_path / "nul.md"
    f.write_bytes(b"a\x00b")
    seen = _capture(monkeypatch)
    out = mcp_server.create_issue("Title", body_file=str(f),
                                  skip_duplicate_check=True)
    assert out["ok"] is False and "NUL byte" in out["stderr"]
    out = mcp_server.comment(42, "a\x00b")
    assert out["ok"] is False and "NUL byte" in out["stderr"]
    assert seen == []


# ---- the real zh script: --stdin carries the body into createIssue ------------

def test_zh_create_stdin_puts_the_exact_body_in_create_issue(monkeypatch, tmp_path) -> None:
    """Run the argv create_issue builds through the real cmd_create, with the
    text on stdin. The body that reaches the createIssue mutation must be the
    text given (bash's $(cat) drops only trailing newlines), and a body of
    "--help" must create an issue rather than print usage."""
    import json

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bash_runner import run_zh_with_stubs

    vars_file = tmp_path / "vars.json"
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        get_workspace_id() { printf 'ws-gid'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-task'; }
        zh_issue_type_names_from() { printf 'Task'; }
        zh_graphql() {
            if [[ "$1" == *createIssue* ]]; then
                printf '%s' "$2" > "__VARS__"
                printf '%s' '{"data":{"createIssue":{"issue":{"id":"new-gid","number":4242,"htmlUrl":"https://example/4242","title":"T","repository":{"ownerName":"acme","name":"widgets"},"issueType":{"name":"Task"}}}}}'
                return 0
            fi
            printf '%s' '{"data":{}}'
        }
    """.replace("__VARS__", str(vars_file))

    for text in (BODY, "--help"):
        seen = _capture(monkeypatch)
        mcp_server.create_issue("Title", text, skip_duplicate_check=True)
        args = seen[0][1:]  # drop the "create" subcommand; cmd_create is called directly
        if vars_file.exists():
            vars_file.unlink()
        r = run_zh_with_stubs(stubs, 'cmd_create "$@"', args=args, stdin=stdins[-1])
        assert vars_file.exists(), f"createIssue never ran for {text!r}: {r.stderr}"
        sent = json.loads(vars_file.read_text(encoding="utf-8"))["input"]["body"]
        assert sent == text.rstrip("\n"), (text, sent)


def test_inline_text_over_the_limit_is_refused(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    big = "a" * (mcp_server._GITHUB_BODY_MAX_CHARS + 1)
    out = mcp_server.create_issue("Title", big, skip_duplicate_check=True)
    assert out["ok"] is False and "character limit" in out["stderr"]
    out = mcp_server.comment(42, big)
    assert out["ok"] is False and "character limit" in out["stderr"]
    assert seen == []


def test_run_zh_returns_a_failure_when_zh_cannot_start(tmp_path) -> None:
    """An exec failure (E2BIG on Linux, a vanished cwd) is reported in the
    standard result shape, never raised out of the tool."""
    r = mcp_server._run_zh(["--version"], cwd=str(tmp_path / "gone"))
    assert r["ok"] is False
    assert r["exit_code"] == -1
    assert r["stderr_plain"].startswith("zh could not be started:")


def test_lone_surrogate_is_refused(monkeypatch) -> None:
    seen = _capture(monkeypatch, stdout="")
    out = mcp_server.comment(42, "bad \ud800 text")
    assert out["ok"] is False and "not valid Unicode" in out["stderr"]
    assert seen == []


def test_zh_comment_succeeds_on_a_large_body(tmp_path) -> None:
    """The real cmd_comment with a body over 128 KiB on stdin must exit 0
    once gh has posted it. A preview built with `echo | head` died of
    SIGPIPE under pipefail here, so zh reported failure for a comment that
    was already posted, and a retry would post it twice."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bash_runner import run_zh_with_stubs

    got = tmp_path / "gh_stdin"
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        gh() { cat > "__GOT__"; echo "https://example/42#issuecomment-1"; }
    """.replace("__GOT__", str(got))
    body = "é" * 65536 + "\n" + "a" * 10000
    r = run_zh_with_stubs(stubs, 'cmd_comment "$@"', args=["42", "--stdin"],
                          stdin=body)
    assert r.returncode == 0, (r.returncode, r.stderr[-300:])
    assert "Added comment to #42" in r.stdout
    assert got.read_text(encoding="utf-8") == body + "\n", "gh did not get the whole body"
