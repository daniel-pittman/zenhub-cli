"""Regression tests for the --help footgun (#102).

`zh create --help` bound `--help` as the first positional and created a real
issue titled "--help". Three separate automated sessions hit it in one day —
two of them after being warned about the edge — which is the argument for a
guard in the tool rather than a note in the docs.

The guard is at the DISPATCHER, so it covers every subcommand at once
(including ones added later) instead of relying on each to remember.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ZH = str(Path(__file__).resolve().parent.parent / "zh")


def _run(args: list[str]):
    return subprocess.run([ZH, *args], capture_output=True, text=True, timeout=60)


def test_create_help_prints_usage_and_creates_nothing() -> None:
    """THE bug: must render help, and must never reach the create path."""
    r = _run(["create", "--help"])
    assert r.returncode == 0
    assert "USAGE" in r.stdout
    assert "Creating issue" not in (r.stdout + r.stderr), "must not attempt a create"


def test_create_short_help_flag() -> None:
    r = _run(["create", "-h"])
    assert r.returncode == 0 and "USAGE" in r.stdout


def test_help_guard_covers_other_free_form_subcommands() -> None:
    """The generalization the ticket asked for: any subcommand binding
    free-form text is protected, not just `create`."""
    for args in (["comment", "42", "--help"], ["epic", "create", "--help"],
                 ["close", "42", "--help"], ["priority", "42", "--help"]):
        r = _run(args)
        assert r.returncode == 0, f"{args} -> rc={r.returncode}"
        assert "USAGE" in r.stdout, f"{args} did not print usage"


def test_self_documenting_commands_keep_their_own_usage() -> None:
    """Commands with specific usage must NOT be shadowed by the general help."""
    for cmd, needle in (("count", "zh count"), ("doctor", "zh doctor"),
                        ("reparent", "zh reparent")):
        r = _run([cmd, "--help"])
        out = r.stdout + r.stderr
        assert needle in out, f"{cmd} --help lost its specific usage: {out[:120]!r}"


def test_flag_shaped_title_is_refused_not_adopted() -> None:
    """A mistyped option must not silently become the issue title."""
    r = _run(["create", "--titel", "oops"])
    assert r.returncode != 0
    assert "Unrecognized option: --titel" in r.stderr


def test_unknown_flag_after_title_is_refused_not_discarded() -> None:
    """Once the title was bound, unknown flags were silently DROPPED, so a
    command looked like it honored a flag that does not exist. Same defect as
    adopting one as the title: proceeding with an argument we don't understand."""
    r = _run(["create", "a real title", "--dry-run"])
    assert r.returncode != 0
    assert "Unrecognized option: --dry-run" in r.stderr
    assert "Creating issue" not in (r.stdout + r.stderr)


def test_missing_title_still_reports_create_usage_not_general_help() -> None:
    """The guard must not swallow the normal no-args usage error."""
    r = _run(["create"])
    assert r.returncode != 0
    assert "Usage: zh create" in r.stderr


# --- usage must not require credentials -------------------------------------

def _run_tokenless(args: list[str], tmp_path):
    """Run zh the way CI does: no ZH_TOKEN and no ~/.config/zh/config."""
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("ZH_TOKEN", "ZH_REST_TOKEN")}
    env["HOME"] = str(tmp_path)
    return subprocess.run([ZH, *args], capture_output=True, text=True, timeout=60, env=env)


def test_help_and_usage_work_without_a_token(tmp_path) -> None:
    """STRUCTURAL: `load_config` aborts when no ZH_TOKEN is set, so calling it
    before argument parsing makes every usage/typo message unreachable without
    credentials — and unreachable in CI, which has no token. These passed
    locally only because a token was configured; CI caught it.

    Telling an operator they mistyped a flag must not require auth.
    """
    # general help via the dispatcher guard
    r = _run_tokenless(["create", "--help"], tmp_path)
    assert r.returncode == 0 and "USAGE" in r.stdout

    # command-specific usage (these self-handle --help)
    for cmd, needle in (("count", "zh count"), ("doctor", "zh doctor"),
                        ("reparent", "zh reparent")):
        out = _run_tokenless([cmd, "--help"], tmp_path)
        assert needle in (out.stdout + out.stderr), (
            f"{cmd} --help required a token: {(out.stdout + out.stderr)[:100]!r}")

    # argument errors, too
    bad = _run_tokenless(["create", "--titel", "x"], tmp_path)
    assert "Unrecognized option: --titel" in bad.stderr

    missing = _run_tokenless(["create"], tmp_path)
    assert "Usage: zh create" in missing.stderr
