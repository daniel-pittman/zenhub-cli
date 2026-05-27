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
