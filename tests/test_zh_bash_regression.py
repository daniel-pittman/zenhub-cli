"""Regression pins for bash-side behaviour that has been broken-then-fixed
in the past. Each test names the round where the bug was introduced and
the round where it was caught, and replicates the production logic
inline. If `zh` changes, the inline snippet here must be updated to
match — keep both in sync.
"""

from __future__ import annotations

import subprocess


_WALKED_NUMS_GUARD_SNIPPET = r"""
post_state="$1"
# Mirrors cmd_sprint_remove's walked_nums sentinel branch (zh ~3744):
#   walked_nums: []  -> "" (legitimate zero-walk, fall through)
#   walked_nums missing / non-array -> "__MISSING__" (structural bug, exit 2)
walked_nums_csv=$(echo "$post_state" | jq -r 'if (.walked_nums | type) == "array" then (.walked_nums | map(tostring) | join(",")) else "__MISSING__" end' 2>/dev/null || echo "__MISSING__")
if [[ "$walked_nums_csv" == "__MISSING__" ]]; then
    echo "STRUCTURAL_BUG"
    exit 2
fi
echo "OK:$walked_nums_csv"
"""


def _run_guard(post_state_json: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _WALKED_NUMS_GUARD_SNIPPET, "_", post_state_json],
        capture_output=True,
        text=True,
        check=False,
    )


def test_walked_nums_empty_array_is_zero_walk_not_structural_bug() -> None:
    """CRITICAL regression pin for cmd_sprint_remove.

    Scenario: user runs `zh sprint remove "Sprint 1" 42` against a
    sprint whose only issue is #42. The removeIssuesFromSprints
    mutation succeeds; the post-mutation walker walks the now-empty
    sprint and emits `walked_nums: []`. The guard MUST fall through
    so the user sees "Removed 1/1 issue(s)", NOT "Walker output is
    missing the 'walked_nums' field (internal bug)" + exit 2.

    History:
      - Round-8 #13 designed the jq emit to draw the distinction:
        empty array -> "", missing/non-array -> "__MISSING__". The
        bash guard tested only the sentinel.
      - Round-10 sweep broadened the guard to
        `[[ -z "$x" || "$x" == "__MISSING__" ]]` while patterning a
        defensive check. That re-conflated the legitimate empty-walk
        case with the structural-bug case. Caught by round-11 review.
      - The fix returns the guard to sentinel-only. This test pins
        the empty-array path so a future sweep can't regress it
        without a test failure.
    """
    result = _run_guard('{"walked_nums": [], "nodes": []}')
    assert result.returncode == 0, (
        f"empty walked_nums must NOT trigger structural-bug branch; "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK:", (
        f"expected empty CSV after fall-through; got {result.stdout!r}"
    )


def test_walked_nums_missing_field_is_structural_bug() -> None:
    """Symmetric pin for the structural-bug path.

    When the walker output genuinely lacks `walked_nums` (or it's
    non-array) the guard MUST fire with exit 2 and the "internal bug"
    message. This is the original round-8 #13 intent; pinned here
    alongside the empty-array case so the distinction is permanently
    load-bearing.
    """
    # `walked_nums` key absent entirely
    result = _run_guard('{"nodes": []}')
    assert result.returncode == 2
    assert result.stdout.strip() == "STRUCTURAL_BUG"

    # `walked_nums` present but non-array
    result = _run_guard('{"walked_nums": "oops"}')
    assert result.returncode == 2
    assert result.stdout.strip() == "STRUCTURAL_BUG"

    # walker output isn't valid JSON (jq errors -> recovery emits sentinel)
    result = _run_guard("not valid json at all")
    assert result.returncode == 2
    assert result.stdout.strip() == "STRUCTURAL_BUG"


def test_walked_nums_populated_array_falls_through_with_csv() -> None:
    """Sanity: populated array path produces a comma-joined CSV and
    falls through. Not strictly a regression pin, but pins the
    overall guard contract so a maintainer reading the test file
    sees the full intended behaviour.
    """
    result = _run_guard('{"walked_nums": [101, 102, 103]}')
    assert result.returncode == 0
    assert result.stdout.strip() == "OK:101,102,103"


# ---------------------------------------------------------------------------
# cmd_delete: confirmation gate + failure-cause surfacing (PR #21, v1.8.0)
#
# `zh delete` is the one irreversible issue-lifecycle verb. Two behaviours
# are pinned here because a regression in either is silently dangerous:
#   1. The confirmation gate only fires for interactive use without -y; a
#      non-interactive caller (agent/pipe/CI) must NEVER block on a prompt,
#      and an interactive caller must NOT delete unless the typed reply
#      matches the issue number exactly.
#   2. On gh failure the real stderr is surfaced (not a hardcoded
#      "permissions" guess), so misdiagnosis can't send the user astray.
#
# The snippet mirrors cmd_delete's gate + delete inline. Interactivity is
# driven by an arg here instead of `-t 0` (a subprocess pipe can't fake a
# TTY); if cmd_delete's logic changes, update this snippet to match.
# ---------------------------------------------------------------------------

_DELETE_SNIPPET = r"""
assume_yes="$1"; is_tty="$2"; gh_exit="$3"; issue_num="$4"
issue_title="Sample title"

# Stub gh: success exits 0 silently; failure emits a non-permissions
# cause on stderr so we can assert it is surfaced verbatim.
gh() {
    if [[ "$gh_exit" == "0" ]]; then return 0; fi
    echo "GraphQL: rate limited, try again later" >&2
    return 1
}

error() { echo "ERROR: $1" >&2; exit 1; }
warn() { echo "WARN: $1" >&2; }
info() { echo "INFO: $1"; }
success() { echo "OK: $1"; }

# Mirrors cmd_delete's confirmation gate (zh cmd_delete):
if [[ "$assume_yes" != "true" && "$is_tty" == "true" ]]; then
    warn "PERMANENTLY delete #${issue_num}: ${issue_title}"
    warn "This cannot be undone."
    read -r reply
    if [[ "$reply" != "$issue_num" ]]; then
        error "Aborted — confirmation did not match. Nothing was deleted."
    fi
fi

info "Deleting issue #${issue_num}: ${issue_title}..."
if gh_err=$(gh issue delete "$issue_num" --yes 2>&1); then
    success "Deleted #${issue_num}: ${issue_title}"
else
    error "Failed to delete issue #${issue_num}: ${gh_err:-(no error output)}"
fi
"""


def _run_delete(
    assume_yes: str, is_tty: str, gh_exit: str, issue_num: str, reply: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _DELETE_SNIPPET, "_", assume_yes, is_tty, gh_exit, issue_num],
        input=reply,
        capture_output=True,
        text=True,
        check=False,
    )


def test_delete_noninteractive_proceeds_without_prompt() -> None:
    """Agent/pipe/CI path: no TTY, no -y -> must NOT prompt, must delete.

    The danger here is a blocking `read` that hangs a non-interactive
    caller forever. The gate must be skipped entirely.
    """
    result = _run_delete("false", "false", "0", "42")
    assert result.returncode == 0
    assert "OK: Deleted #42" in result.stdout
    assert "WARN: PERMANENTLY delete" not in result.stderr


def test_delete_yes_flag_skips_prompt_when_interactive() -> None:
    """-y/--yes bypasses the prompt even on an interactive terminal."""
    result = _run_delete("true", "true", "0", "42")
    assert result.returncode == 0
    assert "OK: Deleted #42" in result.stdout
    assert "WARN: PERMANENTLY delete" not in result.stderr


def test_delete_interactive_aborts_on_mismatched_confirmation() -> None:
    """Interactive, no -y, wrong reply -> abort, nothing deleted."""
    result = _run_delete("false", "true", "0", "42", reply="999\n")
    assert result.returncode == 1
    assert "Aborted" in result.stderr
    assert "OK: Deleted" not in result.stdout


def test_delete_interactive_proceeds_on_matching_confirmation() -> None:
    """Interactive, no -y, reply matches issue number -> delete."""
    result = _run_delete("false", "true", "0", "42", reply="42\n")
    assert result.returncode == 0
    assert "OK: Deleted #42" in result.stdout


def test_delete_failure_surfaces_real_gh_stderr() -> None:
    """On gh failure the actual cause is surfaced, not a hardcoded
    permissions guess. Pins the v1.8.0 fix to PR #21 review finding #2.
    """
    result = _run_delete("true", "false", "1", "42")
    assert result.returncode == 1
    assert "Failed to delete issue #42" in result.stderr
    assert "rate limited" in result.stderr


# zh runs under `set -euo pipefail`. The title pre-fetch is a bare
# assignment from a `gh issue view` that exits non-zero when the issue
# does not exist. Without the `|| true` guard, set -e kills the script at
# the assignment (silent exit 1) BEFORE the not-found `error` can print
# its helpful message. This snippet mirrors that pre-fetch + guard under a
# real `set -euo pipefail` with a failing `gh` stub, and pins that the
# helpful message is reached. The same fix was applied to cmd_close and
# cmd_reopen, which shared the identical latent pattern.
_DELETE_NOTFOUND_SNIPPET = r"""
set -euo pipefail
issue_num="$1"
owner_repo="owner/repo"
gh() { return 1; }   # 'issue view' fails -> issue does not exist
error() { echo "ERROR: $1" >&2; exit 1; }

issue_title=""
issue_title=$(gh issue view "$issue_num" --repo "$owner_repo" --json title --jq '.title' 2>/dev/null) || true
if [[ -z "$issue_title" ]]; then
    error "Issue #${issue_num} not found in ${owner_repo}."
fi
echo "REACHED_DELETE"
"""


def test_delete_notfound_reaches_guard_under_set_e() -> None:
    """Regression pin (PR #21): the not-found guard must fire its message
    rather than letting set -e kill the script silently at the title
    pre-fetch assignment.
    """
    result = subprocess.run(
        ["bash", "-c", _DELETE_NOTFOUND_SNIPPET, "_", "999999"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Issue #999999 not found" in result.stderr
    assert "REACHED_DELETE" not in result.stdout
