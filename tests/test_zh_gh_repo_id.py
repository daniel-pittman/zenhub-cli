"""Regression tests for `resolve_gh_repo_id` (v1.9.5).

`gh api repos/<owner>/<repo> --jq '.id'` does not fail safely on its
own. On an HTTP error it either exits non-zero (a well-behaved gh on a
404) or, when a broken PATH / shim resolves a misbehaving gh, exits 0
while printing the JSON error body to stdout, e.g.

    {"message":"Not Found","documentation_url":"...","status":"404"}

The pre-v1.9.5 call sites guarded only with `[[ -z "$gh_id" ]]`, which:

  * misses the body case: the non-numeric blob is non-empty, so it flows
    into the GraphQL `$ghIds: [Int!]!` variable and surfaces as a
    baffling "could not coerce value {...} to Int" cascade, followed by
    a misleading "No workspace found for this repository";
  * and under `set -euo pipefail` the non-zero case dies silently at the
    bare assignment before the guard's message can print.

`resolve_gh_repo_id` fails closed: it checks BOTH the gh exit status and
that the result is purely numeric. These tests drive the REAL helper and
a REAL caller (`get_repo_id`) via the production-sourcing harness.
"""

from __future__ import annotations

from _bash_runner import run_zh_with_stubs

# A gh that exits 0 but prints the 404 error body to stdout. This is the
# exact shape that produced the user-reported cascade: a broken PATH after
# a dep reinstall resolved a gh that did not fail closed.
_GH_BODY_EXIT0 = (
    r"""gh() { printf '%s' '{"message":"Not Found","""
    r""""documentation_url":"https://docs.github.com/rest","status":"404"}'; return 0; }"""
)

# A well-behaved gh that exits non-zero on a 404 (and, with --jq, also
# prints the error body to stdout).
_GH_BODY_EXIT1 = (
    r"""gh() { printf '%s' '{"message":"Not Found","status":"404"}'; return 1; }"""
)

# A healthy gh: numeric id on stdout, clean exit.
_GH_NUMERIC = r"""gh() { printf '%s' '12345'; }"""


def test_resolve_gh_repo_id_rejects_body_with_zero_exit() -> None:
    """The misbehaving-gh case (exit 0 + JSON error body) must fail
    closed: the helper returns 1 and prints nothing numeric to stdout."""
    r = run_zh_with_stubs(_GH_BODY_EXIT0, "resolve_gh_repo_id acme/widgets")
    assert r.returncode == 1
    assert r.stdout.strip() == ""
    # The collapsed gh diagnostic is surfaced to stderr, not swallowed.
    assert "Not Found" in r.stderr


def test_resolve_gh_repo_id_rejects_nonzero_exit() -> None:
    """The well-behaved-404 case (non-zero exit) must also fail closed
    rather than dying silently under set -e."""
    r = run_zh_with_stubs(_GH_BODY_EXIT1, "resolve_gh_repo_id acme/widgets")
    assert r.returncode == 1
    assert r.stdout.strip() == ""


def test_resolve_gh_repo_id_returns_numeric_id_on_success() -> None:
    """The happy path: a numeric id is echoed verbatim with no trailing
    newline so callers can pass it straight to jq --argjson."""
    r = run_zh_with_stubs(_GH_NUMERIC, "resolve_gh_repo_id acme/widgets")
    assert r.returncode == 0
    assert r.stdout == "12345"


def test_get_repo_id_clean_error_no_graphql_cascade() -> None:
    """End-to-end pin: with the misbehaving gh, the REAL get_repo_id must
    abort with a clean actionable message BEFORE ever building the
    GraphQL variables. Proves the "could not coerce ... to Int" cascade
    can no longer happen: zh_graphql is never reached."""
    stubs = _GH_BODY_EXIT0 + "\n" + r"""zh_graphql() { printf 'GRAPHQL_WAS_CALLED'; }"""
    r = run_zh_with_stubs(stubs, "get_repo_id acme/widgets")
    assert r.returncode == 1
    assert "Could not get GitHub repo ID for: acme/widgets" in r.stderr
    assert "gh auth status" in r.stderr
    # The cascade markers must be absent: GraphQL was never called, so no
    # coercion error can be produced from the error body.
    assert "GRAPHQL_WAS_CALLED" not in r.stdout
    assert "coerce" not in (r.stdout + r.stderr)


def test_fetch_issue_types_degrades_to_empty_array_on_gh_failure() -> None:
    """The soft path: zh_fetch_issue_types must degrade to "[]" rather
    than aborting when the repo id cannot be resolved. Mirrors the
    pre-existing contract (echo "[]"; return) now routed through the
    fail-closed helper."""
    stubs = _GH_BODY_EXIT0
    r = run_zh_with_stubs(stubs, "zh_fetch_issue_types acme/widgets ws-gid")
    assert r.returncode == 0
    assert r.stdout.strip() == "[]"
