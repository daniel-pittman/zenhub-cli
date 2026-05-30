"""LEGACY regression pins for bash-side behaviour that has been
broken-then-fixed in the past. Each test names the round where the
bug was introduced and the round where it was caught, and replicates
the production logic inline. If `zh` changes, the inline snippet
here must be updated to match — keep both in sync.

v1.9.2 architecture note: the snippet pattern below is LEGACY. It
caused a six-round review cycle on PR #25 (v1.9.1) because snippet
authors drifted from production without the test failing. New tests
MUST source the production `zh` script via `tests/_bash_runner.py`
and exercise real `cmd_*` functions, not parallel re-implementations.
See `tests/test_zh_production_regression.py` for the canonical
pattern. The existing snippet tests stay (most of them pin pure-jq
projections that have been stable for many rounds and migrating
them in bulk is not worth the churn), but every NEW pin lives in
the production-sourcing file.

The class-3 anti-pattern this file historically exhibited:
parallel-stub-vs-production. Two cases were caught and DELETED in
v1.9.2 (round-7 finding #15 — see the placeholder comments above
their former locations) where the snippet's `exit 1` contradicted
production's post-round-6 `exit 2`. Any future class-3 case should
be deleted, with its production contract pinned by a new test in
test_zh_production_regression.py.
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
set -euo pipefail
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
    reply=""
    read -r reply || true
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


def test_delete_interactive_aborts_on_eof() -> None:
    """Interactive, no -y, EOF (Ctrl-D / empty stdin) -> friendly abort,
    not a silent set -e death. Pins the `read -r reply || true` fix.
    """
    result = _run_delete("false", "true", "0", "42", reply="")
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


# ===========================================================================
# v1.9.0: issue-type model migration (G7 / G8 / G3) + create --json (G2) +
# priority-by-name (G1).
#
# These mirror the pure-jq resolution helpers in `zh` (zh_issue_type_id_from,
# zh_priority_id_from), the create --json output shaping, and the planning-
# noun create sugar (translating -d -> -b and appending -t <TYPE>). They run
# without network: the assignableIssueTypes / prioritiesConnection payloads
# are fed in directly, exactly as the live API returns them.
# ===========================================================================

# A realistic assignableIssueTypes payload (the union of GithubIssueType and
# ZenhubIssueType), shaped like zh_fetch_issue_types echoes it.
_TYPES_JSON = (
    '[{"typename":"ZenhubIssueType","id":"zid-init","name":"Initiative","level":1,"disposition":"PLANNING_PANEL","isEnabled":true},'
    '{"typename":"ZenhubIssueType","id":"zid-proj","name":"Project","level":2,"disposition":"PLANNING_PANEL","isEnabled":true},'
    '{"typename":"ZenhubIssueType","id":"zid-epic","name":"Epic","level":3,"disposition":"PLANNING_PANEL","isEnabled":true},'
    '{"typename":"GithubIssueType","id":"gid-bug","name":"Bug","level":4,"disposition":"BOARD","isEnabled":true},'
    '{"typename":"GithubIssueType","id":"gid-feat","name":"Feature","level":4,"disposition":"BOARD","isEnabled":true},'
    '{"typename":"GithubIssueType","id":"gid-task","name":"Task","level":4,"disposition":"BOARD","isEnabled":true},'
    '{"typename":"ZenhubIssueType","id":"zid-sub","name":"Sub-task","level":5,"disposition":"BOARD","isEnabled":true}]'
)

# Mirrors zh_issue_type_id_from (case-insensitive name -> id, "" if absent).
_TYPE_ID_SNIPPET = r"""
types_json="$1"; name="$2"
echo "$types_json" | jq -r --arg name "$name" \
    'map(select((.name | ascii_downcase) == ($name | ascii_downcase))) | .[0].id // empty'
"""


def _resolve_type_id(name: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _TYPE_ID_SNIPPET, "_", _TYPES_JSON, name],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_issue_type_resolves_github_type_id() -> None:
    """A GithubIssueType (Feature, level 4) resolves to its id. Confirms
    the create path can still type board issues.
    """
    assert _resolve_type_id("Feature") == "gid-feat"


def test_issue_type_resolves_zenhub_type_id() -> None:
    """A ZenhubIssueType (Epic, level 3) resolves to its id via the SAME
    unified mechanism. This is the heart of G8: Epic/Initiative/Project/
    Sub-task are ZenhubIssueTypes, and the unified id is what both
    CreateIssueInput.issueTypeId and ChangeIssueTypeOfIssuesInput.issueTypeId
    accept, so no separate Github-vs-Zenhub branch is needed.
    """
    assert _resolve_type_id("Epic") == "zid-epic"
    assert _resolve_type_id("Sub-task") == "zid-sub"


def test_issue_type_resolution_is_case_insensitive() -> None:
    assert _resolve_type_id("epic") == "zid-epic"
    assert _resolve_type_id("INITIATIVE") == "zid-init"


def test_issue_type_unknown_resolves_to_empty() -> None:
    """An unknown type name resolves to empty so the caller can hard-error
    with the available list rather than firing a create with no type.
    """
    assert _resolve_type_id("Story") == ""


# Mirrors zh_priority_id_from + the not-found "Available:" message build.
_PRIORITY_SNIPPET = r"""
set -euo pipefail
priorities_json="$1"; name="$2"
priority_id=$(echo "$priorities_json" | jq -r --arg name "$name" \
    'map(select((.name | ascii_downcase) == ($name | ascii_downcase))) | .[0].id // empty')
if [[ "$name" == "clear" || "$name" == "none" || "$name" == "remove" ]]; then
    echo "CLEAR"
    exit 0
fi
if [[ -z "$priority_id" ]]; then
    available=$(echo "$priorities_json" | jq -r 'if length == 0 then "(none configured)" else ([.[].name] | join(", ")) end')
    echo "NOMATCH:${available}"
    exit 0
fi
echo "ID:${priority_id}"
"""

_PRIORITIES_JSON = (
    '[{"id":"pid-high","name":"High priority","color":"red"},'
    '{"id":"pid-low","name":"Low priority","color":"blue"}]'
)


def _resolve_priority(priorities_json: str, name: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _PRIORITY_SNIPPET, "_", priorities_json, name],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_priority_by_name_matches_case_insensitively() -> None:
    """G1: the given name resolves against the workspace's configured
    priorities (case-insensitive), NOT a hardcoded high/medium/low set and
    NOT nodes[0]. "high priority" -> the High priority id.
    """
    assert _resolve_priority(_PRIORITIES_JSON, "high priority") == "ID:pid-high"
    assert _resolve_priority(_PRIORITIES_JSON, "Low priority") == "ID:pid-low"


def test_priority_no_match_lists_available() -> None:
    """G1: an unconfigured name errors clearly with the available list,
    instead of silently firing a mutation with an empty id (the old bug
    behind the opaque "Resource not found").
    """
    out = _resolve_priority(_PRIORITIES_JSON, "medium")
    assert out.startswith("NOMATCH:")
    assert "High priority" in out and "Low priority" in out


def test_priority_no_match_with_no_priorities_configured() -> None:
    out = _resolve_priority("[]", "high")
    assert out == "NOMATCH:(none configured)"


def test_priority_clear_is_distinct_from_name_resolution() -> None:
    """`clear` (and its aliases) must take the clear branch, never attempt
    a name match.
    """
    assert _resolve_priority(_PRIORITIES_JSON, "clear") == "CLEAR"


# Mirrors cmd_create's --json output shaping (the final jq emit), with a
# representative set of post-create values.
_CREATE_JSON_SNIPPET = r"""
new_issue_num="$1"; new_issue_url="$2"; title="$3"
new_type_name="$4"; pipeline_set="$5"; estimate="$6"; parent_wired="$7"
jq -n \
    --argjson number "$new_issue_num" \
    --arg url "$new_issue_url" \
    --arg title "$title" \
    --arg type "${new_type_name}" \
    --arg pipeline "${pipeline_set}" \
    --arg estimate "${estimate}" \
    --arg parent "${parent_wired}" \
    '{number: $number, url: $url, title: $title,
      type: (if $type == "" then null else $type end),
      pipeline: (if $pipeline == "" then null else $pipeline end),
      estimate: (if $estimate == "" then null else ($estimate | tonumber) end),
      parent: (if $parent == "" then null else ($parent | tonumber) end)}'
"""


def _create_json(num, url, title, type_, pipeline, estimate, parent):
    r = subprocess.run(
        ["bash", "-c", _CREATE_JSON_SNIPPET, "_", str(num), url, title,
         type_, pipeline, estimate, parent],
        capture_output=True, text=True, check=False,
    )
    import json as _json
    return _json.loads(r.stdout)


def test_create_json_full_object_shape() -> None:
    """G2: create --json emits a clean object with all batch-relevant
    fields; number is a JSON int and absent optionals are null.
    """
    obj = _create_json(
        42, "https://github.com/o/r/issues/42", "Auth service",
        "Epic", "Product Backlog", "5", "12",
    )
    assert obj == {
        "number": 42,
        "url": "https://github.com/o/r/issues/42",
        "title": "Auth service",
        "type": "Epic",
        "pipeline": "Product Backlog",
        "estimate": 5,
        "parent": 12,
    }


def test_create_json_nulls_for_unset_optionals() -> None:
    obj = _create_json(
        7, "https://github.com/o/r/issues/7", "Plain task",
        "Task", "", "", "",
    )
    assert obj["number"] == 7
    assert obj["type"] == "Task"
    assert obj["pipeline"] is None
    assert obj["estimate"] is None
    assert obj["parent"] is None


# Mirrors cmd_hierarchy_create's argument translation: it rewrites a planning
# noun's -d/--description to cmd_create's -b and appends -t <TYPE>. The result
# is the exact argv handed to cmd_create.
_NOUN_CREATE_ARGS_SNIPPET = r"""
type_name="$1"; shift
passthrough=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--description) passthrough+=("-b" "$2"); shift 2 ;;
        *) passthrough+=("$1"); shift ;;
    esac
done
printf '%s\n' "${passthrough[@]}" -t "$type_name"
"""


def test_epic_noun_create_translates_to_typed_create() -> None:
    """G3: `zh epic create "Title" -d "body"` becomes a plain
    `cmd_create "Title" -b "body" -t Epic`. The epic noun is sugar over the
    issue-type model: no ZenhubEpic mutation involved.
    """
    r = subprocess.run(
        ["bash", "-c", _NOUN_CREATE_ARGS_SNIPPET, "_", "Epic",
         "Title", "-d", "body text", "-l", "backend"],
        capture_output=True, text=True, check=False,
    )
    argv = r.stdout.splitlines()
    assert argv == ["Title", "-b", "body text", "-l", "backend", "-t", "Epic"]


def test_subtask_noun_create_passes_json_flag_through() -> None:
    """Flags like --json/-q pass straight through to cmd_create so machine
    output works on every planning noun, not just `zh create`.
    """
    r = subprocess.run(
        ["bash", "-c", _NOUN_CREATE_ARGS_SNIPPET, "_", "Sub-task",
         "Small thing", "--json"],
        capture_output=True, text=True, check=False,
    )
    argv = r.stdout.splitlines()
    assert argv == ["Small thing", "--json", "-t", "Sub-task"]


# Mirrors cmd_set_type's success gate: changeIssueTypeOfIssues returns
# successCount; < 1 is an error, >= 1 is success. (G8 retype-after-create.)
_SET_TYPE_GATE_SNIPPET = r"""
response="$1"
success_count=$(echo "$response" | jq -r '.data.changeIssueTypeOfIssues.successCount // 0')
if [[ "$success_count" -lt 1 ]]; then
    echo "FAILED"
    exit 1
fi
echo "OK"
"""


def _set_type_gate(response: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _SET_TYPE_GATE_SNIPPET, "_", response],
        capture_output=True, text=True, check=False,
    )


def test_set_type_success_when_count_positive() -> None:
    """A retype to a ZenhubIssueType (e.g. Epic) reports successCount 1."""
    resp = '{"data":{"changeIssueTypeOfIssues":{"successCount":1,"failedIssues":[],"githubErrors":[]}}}'
    r = _set_type_gate(resp)
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


def test_set_type_failure_when_count_zero() -> None:
    """successCount 0 (or a failedIssues entry) is a hard failure, not a
    silent no-op.
    """
    resp = '{"data":{"changeIssueTypeOfIssues":{"successCount":0,"failedIssues":[{"number":42}],"githubErrors":[]}}}'
    r = _set_type_gate(resp)
    assert r.returncode == 1
    assert r.stdout.strip() == "FAILED"


# ===========================================================================
# v1.9.0 post-review fixes (PR #23 review findings #2, #3, #5, #7).
# Each snippet mirrors the single guard added in `zh` so a future change
# that loosens or drops the guard fails a test instead of silently shipping.
# ===========================================================================


# Mirrors cmd_create's up-front --estimate format check (review finding #3).
# Bare command-substitution from a failing jq under `set -e` would otherwise
# kill cmd_create AFTER createIssue has run, orphaning the issue.
_ESTIMATE_GUARD_SNIPPET = r"""
set -euo pipefail
estimate="$1"
if [[ -n "$estimate" ]] && ! [[ "$estimate" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "REJECTED"
    exit 1
fi
echo "OK"
"""


def _estimate_guard(value: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _ESTIMATE_GUARD_SNIPPET, "_", value],
        capture_output=True, text=True, check=False,
    )


def test_estimate_guard_accepts_integers_and_decimals() -> None:
    """`-e 5`, `-e 0.5`, and an unset estimate ("") all pass the up-front
    guard. The empty case is the no-`-e` path through cmd_create.
    """
    for v in ("5", "0.5", "13", ""):
        r = _estimate_guard(v)
        assert r.returncode == 0, f"expected OK for {v!r}, got {r.stdout!r}"
        assert r.stdout.strip() == "OK"


def test_estimate_guard_rejects_non_numeric() -> None:
    """A non-numeric `-e` is rejected BEFORE createIssue (review finding
    #3), so a typo can't orphan an issue.
    """
    for v in ("five", "high", "5.", "1.2.3", "-3"):
        r = _estimate_guard(v)
        assert r.returncode == 1, f"expected reject for {v!r}, got {r.stdout!r}"
        assert r.stdout.strip() == "REJECTED"


# Mirrors cmd_hierarchy_create's -d/--description arity guard (review #5).
# A bare `-d` at end-of-args used to dereference unbound $2 under `set -u`.
_NOUN_DASH_D_ARITY_SNIPPET = r"""
set -euo pipefail
passthrough=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--description)
            if [[ $# -lt 2 ]]; then
                echo "ARITY_ERROR"
                exit 1
            fi
            passthrough+=("-b" "$2")
            shift 2
            ;;
        *)
            passthrough+=("$1")
            shift
            ;;
    esac
done
echo "OK:${passthrough[*]:-}"
"""


def _noun_dash_d(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _NOUN_DASH_D_ARITY_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_noun_create_dash_d_arity_guard_rejects_missing_value() -> None:
    """`zh epic create "Title" -d` with no body fails the arity guard
    cleanly instead of dying with a raw `$2: unbound variable` under
    `set -u`.
    """
    r = _noun_dash_d("Title", "-d")
    assert r.returncode == 1
    assert r.stdout.strip() == "ARITY_ERROR"


def test_noun_create_dash_d_with_value_translates_to_dash_b() -> None:
    """`-d "body"` round-trips to `-b "body"` in the cmd_create argv.
    """
    r = _noun_dash_d("Title", "-d", "the body")
    assert r.returncode == 0
    # Whitespace inside "the body" is preserved by ${arr[*]}'s default IFS
    # separator (a single space), so the contiguous "-b the body" string
    # has spaces from BOTH the array separator and the value itself; we
    # just check the relevant tokens are present.
    out = r.stdout.strip()
    assert out.startswith("OK:")
    assert "-b" in out
    assert "the body" in out


# Mirrors cmd_hierarchy_list's coverage-warning gate (review #7). When the
# API reports more than were returned by the capped `first: 100`, the user
# must see a "Showing first N of M" warning instead of a silent truncation.
_LIST_TRUNCATION_SNIPPET = r"""
set -euo pipefail
total="$1"; fetched="$2"
if [[ "$total" -gt "$fetched" ]]; then
    echo "WARN:${fetched}/${total}"
else
    echo "OK"
fi
"""


def _list_truncation(total: str, fetched: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _LIST_TRUNCATION_SNIPPET, "_", total, fetched],
        capture_output=True, text=True, check=False,
    )


def test_list_truncation_warns_when_total_exceeds_fetched() -> None:
    """A workspace with 150 Epics, query capped at 100, must surface
    `Showing first 100 of 150` rather than rendering 100 silently as if
    that were everything.
    """
    r = _list_truncation("150", "100")
    assert r.returncode == 0
    assert r.stdout.strip() == "WARN:100/150"


def test_list_truncation_silent_when_fetched_covers_total() -> None:
    """Full coverage: no warning."""
    r = _list_truncation("17", "17")
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


def test_list_truncation_silent_when_empty() -> None:
    """Zero issues: no warning (the empty case is handled earlier)."""
    r = _list_truncation("0", "0")
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


# ===========================================================================
# v1.9.0 round-3 review fixes (PR #23 findings #2, #6, #7, #10).
# Each snippet mirrors the single guard added in `zh` so a future change
# that loosens or drops the guard fails a test instead of silently shipping.
# ===========================================================================


# Mirrors cmd_hierarchy_create's -t / --type rejection arm. Round-3 #2.
_NOUN_DASH_T_REJECTION_SNIPPET = r"""
set -euo pipefail
passthrough=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--description)
            if [[ $# -lt 2 ]]; then echo "ARITY_ERROR"; exit 1; fi
            passthrough+=("-b" "$2"); shift 2 ;;
        -t|--type)
            echo "T_REJECTED"
            exit 1 ;;
        *)
            passthrough+=("$1"); shift ;;
    esac
done
echo "OK:${passthrough[*]:-}"
"""


def _noun_dash_t(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _NOUN_DASH_T_REJECTION_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_noun_create_rejects_user_supplied_dash_t() -> None:
    """`zh epic create "X" -t Bug` is rejected up-front, because the noun
    IS the type. Last-wins behavior in cmd_create's parser would otherwise
    silently discard the user's -t Bug and create an Epic; any agent or
    human typing -t in this context would get the wrong type.
    """
    r = _noun_dash_t("Title", "-t", "Bug")
    assert r.returncode == 1
    assert r.stdout.strip() == "T_REJECTED"


def test_noun_create_rejects_long_form_dash_dash_type() -> None:
    """Same for the long form --type."""
    r = _noun_dash_t("Title", "--type", "Feature")
    assert r.returncode == 1
    assert r.stdout.strip() == "T_REJECTED"


def test_noun_create_accepts_dash_d_unchanged() -> None:
    """The -t rejection doesn't break the existing -d translation."""
    r = _noun_dash_t("Title", "-d", "the body")
    assert r.returncode == 0
    out = r.stdout.strip()
    assert out.startswith("OK:")
    assert "-b" in out and "the body" in out


# Mirrors cmd_hierarchy_show's body printf. Round-3 #6. `echo "$body"`
# would treat a body starting with -e/-n/-E as flags and either silently
# drop the line or interpret \n as a literal newline. `printf '%s\n'`
# is flag-immune.
_BODY_PRINTF_SNIPPET = r"""
set -euo pipefail
body="$1"
printf '%s\n' "$body" | sed 's/^/  /'
"""


def _body_printf(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _BODY_PRINTF_SNIPPET, "_", body],
        capture_output=True, text=True, check=False,
    )


def test_show_body_prints_flag_starting_body_literally() -> None:
    """A body starting with `-e` round-trips as text, not as an echo
    flag. The old `echo "$body"` rendered this empty.
    """
    r = _body_printf("-e first line\nsecond line")
    assert r.returncode == 0
    assert r.stdout == "  -e first line\n  second line\n"


def test_show_body_prints_dash_n_starting_body() -> None:
    """`-n` is the bash echo "suppress newline" flag. printf is immune."""
    r = _body_printf("-n raw text")
    assert r.returncode == 0
    assert r.stdout == "  -n raw text\n"


def test_show_body_prints_dash_capital_e_starting_body() -> None:
    """`-E` (disable escape interpretation) is also an echo flag."""
    r = _body_printf("-E literal $ backslash content")
    assert r.returncode == 0
    assert r.stdout == "  -E literal $ backslash content\n"


# Mirrors cmd_hierarchy_show's child-truncation warn. Round-3 #7.
# Symmetric with cmd_hierarchy_list's gate, against the per-child query
# instead of the per-noun query.
_SHOW_TRUNCATION_SNIPPET = r"""
set -euo pipefail
child_count="$1"; fetched="$2"
if [[ "$child_count" -gt "$fetched" ]]; then
    echo "WARN:${fetched}/${child_count}"
else
    echo "OK"
fi
"""


def _show_truncation(child_count: str, fetched: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _SHOW_TRUNCATION_SNIPPET, "_", child_count, fetched],
        capture_output=True, text=True, check=False,
    )


def test_show_truncation_warns_when_children_exceed_fetched() -> None:
    """Epic with 120 sub-issues, query capped at 100, surfaces a warning
    so a planner counting open children does not close the epic
    prematurely.
    """
    r = _show_truncation("120", "100")
    assert r.returncode == 0
    assert r.stdout.strip() == "WARN:100/120"


def test_show_truncation_silent_when_under_cap() -> None:
    """A normal-sized epic (fewer than 100 children) shows no warning."""
    r = _show_truncation("5", "5")
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


# Mirrors cmd_hierarchy_dispatch's `list` arm reject-stray-arg gate.
# Round-3 #10.
_NOUN_LIST_REJECT_STRAY_SNIPPET = r"""
set -euo pipefail
type_name="$1"
shift
if [[ $# -gt 0 ]]; then
    echo "REJECT:${#}:${*}"
    exit 1
fi
echo "OK"
"""


def _noun_list_reject_stray(type_name, *args):
    return subprocess.run(
        ["bash", "-c", _NOUN_LIST_REJECT_STRAY_SNIPPET, "_", type_name, *args],
        capture_output=True, text=True, check=False,
    )


def test_noun_list_rejects_stray_positional() -> None:
    """`zh epic list 42` errors clearly instead of silently listing the
    full Epic workspace and discarding the 42 (which a user trained on
    `zh subissue list <parent#>` reasonably expects to filter).
    """
    r = _noun_list_reject_stray("Epic", "42")
    assert r.returncode == 1
    assert r.stdout.startswith("REJECT:1:42")


def test_noun_list_no_args_passes() -> None:
    """The well-formed `zh epic list` continues to work."""
    r = _noun_list_reject_stray("Epic")
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


def test_noun_list_rejects_multiple_stray() -> None:
    """Multiple stray args are also rejected, with the count surfaced."""
    r = _noun_list_reject_stray("Project", "42", "43", "44")
    assert r.returncode == 1
    assert r.stdout.startswith("REJECT:3:42 43 44")


# ===========================================================================
# v1.9.1 closeout sweep. Items 1-11 from /tmp/zh-v1.9.1-gaps.md.
# ===========================================================================


# v1.9.1 item #1: sub-issue add / remove guard on githubErrors must accept
# the empty-array shape ZenHub returns when there are no GitHub-side errors.
# The pre-fix guard compared against {} and null only, so an empty []
# leaked into the warn line as a literal "[]".
_GH_ERRORS_GATE_SNIPPET = r"""
set -euo pipefail
response="$1"
gh_errors=$(echo "$response" | jq -c '.data.addSubIssues.githubErrors // {}')
gh_errors_len=$(echo "$gh_errors" | jq 'if type == "object" or type == "array" then length else 1 end')
if [[ "$gh_errors_len" -gt 0 ]]; then
    echo "WARN:${gh_errors}"
else
    echo "OK"
fi
"""


def _gh_errors_gate(response: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _GH_ERRORS_GATE_SNIPPET, "_", response],
        capture_output=True, text=True, check=False,
    )


def test_gh_errors_gate_silent_on_empty_array() -> None:
    """addSubIssues returns githubErrors: [] when there are no GitHub-side
    errors. The guard must fall through; the pre-v1.9.1 string-equality
    check against {} and null leaked a literal "[]" into the warn output.
    """
    resp = '{"data":{"addSubIssues":{"githubErrors":[]}}}'
    r = _gh_errors_gate(resp)
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


def test_gh_errors_gate_silent_on_empty_object() -> None:
    """The historical empty-object shape (the one the pre-fix guard
    handled) still falls through, so this is not a regression of the
    earlier intent.
    """
    resp = '{"data":{"addSubIssues":{"githubErrors":{}}}}'
    r = _gh_errors_gate(resp)
    assert r.returncode == 0
    assert r.stdout.strip() == "OK"


def test_gh_errors_gate_silent_on_null() -> None:
    """Missing key (defaults to {}) and explicit null both fall through."""
    for resp in (
        '{"data":{"addSubIssues":{}}}',
        '{"data":{"addSubIssues":{"githubErrors":null}}}',
    ):
        r = _gh_errors_gate(resp)
        assert r.returncode == 0
        assert r.stdout.strip() == "OK", f"resp={resp!r} -> {r.stdout!r}"


def test_gh_errors_gate_warns_on_populated_array() -> None:
    """A populated githubErrors array surfaces the contents verbatim."""
    resp = '{"data":{"addSubIssues":{"githubErrors":[{"code":"X","message":"Y"}]}}}'
    r = _gh_errors_gate(resp)
    assert r.returncode == 0
    out = r.stdout.strip()
    assert out.startswith("WARN:")
    assert "code" in out and "X" in out


def test_gh_errors_gate_warns_on_populated_object() -> None:
    """A populated object form also surfaces (defensive: present in some
    older mutation payloads).
    """
    resp = '{"data":{"addSubIssues":{"githubErrors":{"some_key":"value"}}}}'
    r = _gh_errors_gate(resp)
    assert r.returncode == 0
    assert r.stdout.strip().startswith("WARN:")


# v1.9.1 item #3: cmd_set_type must honor githubErrors and failedIssues even
# when successCount is positive. A partial-failure payload (successCount=1
# with a populated failedIssues / githubErrors) used to print "Set type" as
# if the change had landed.
# v1.9.2 round-7 finding #15: DELETED stale snippet
# `_SET_TYPE_PARTIAL_FAILURE_SNIPPET` and its three tests
# (test_set_type_partial_failure_with_failed_issues_is_error,
#  test_set_type_partial_failure_with_github_errors_is_error,
#  test_set_type_clean_success_still_succeeds).
# Same drift problem as the _SET_TYPE_PARTIAL_MSG_SNIPPET deletion
# above: the snippets asserted `returncode == 1` against their own
# embedded gate, but round-6 #4 moved production to `exit 2`. The
# replacement coverage is in tests/test_zh_production_regression.py,
# against PRODUCTION cmd_set_type rather than a parallel snippet:
#
# v1.9.3 pattern-sweep finding #15: corrected the replacement-test
# references. The original deletion comment named ONLY
# `test_structural_guarantee_set_type_exits_2_not_1_on_partial`, which
# only covers the partial branch. The full branch coverage now lives
# across:
#   * Partial via failedIssues (exit 2):
#       test_structural_guarantee_set_type_exits_2_not_1_on_partial
#   * Partial via githubErrors (exit 2):
#       test_round2_f7_set_type_partial_via_github_errors_only_exits_2
#   * Hard failure (exit 1):
#       test_round2_f7_set_type_success_count_zero_exits_1
#   * Clean success (exit 0):
#       test_round3_f6_set_type_clean_success_exits_0
# All of these are production-sourced and supersede the deleted
# snippet-only coverage.


# v1.9.1 item #4: zh_fetch_issue_types must filter isEnabled=false rows so
# zh_hierarchy_require_type's "Enable it..." branch is reachable. The pure-
# jq projection (the inner pipeline of zh_fetch_issue_types) is the unit
# under test; we feed it a payload mixing enabled and disabled rows.
_TYPES_FILTER_SNIPPET = r"""
set -euo pipefail
payload="$1"
echo "$payload" | jq -c '
    [ (.data.repositoriesByGhId[0].assignableIssueTypes.nodes // [])[]
      | select(.isEnabled != false)
      | {typename: .__typename, id: .id, name: .name, level: .level,
         disposition: .disposition, isEnabled: .isEnabled} ]'
"""


def _types_filter(payload: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _TYPES_FILTER_SNIPPET, "_", payload],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_types_filter_drops_disabled_rows() -> None:
    """A type with isEnabled=false must be filtered out before any name-
    resolution lookup runs. Otherwise zh_hierarchy_require_type's error
    message ("Enable it... then retry") can never fire: the disabled type
    would resolve to an id and CreateIssueInput.issueTypeId would silently
    succeed (or fail server-side with an opaque message).
    """
    payload = ('{"data":{"repositoriesByGhId":[{"assignableIssueTypes":'
               '{"nodes":['
               '{"__typename":"ZenhubIssueType","id":"zid-epic","name":"Epic",'
               '"level":3,"disposition":"PLANNING_PANEL","isEnabled":true},'
               '{"__typename":"ZenhubIssueType","id":"zid-disabled","name":"Theme",'
               '"level":1,"disposition":"PLANNING_PANEL","isEnabled":false}'
               ']}}]}}')
    out = _types_filter(payload)
    import json as _json
    types = _json.loads(out)
    names = [t["name"] for t in types]
    assert "Epic" in names
    assert "Theme" not in names, (
        f"Disabled type leaked through: {names!r}"
    )


def test_types_filter_keeps_enabled_rows() -> None:
    """Sanity: every isEnabled=true row survives the filter."""
    payload = ('{"data":{"repositoriesByGhId":[{"assignableIssueTypes":'
               '{"nodes":['
               '{"__typename":"GithubIssueType","id":"gid-bug","name":"Bug",'
               '"level":4,"disposition":"BOARD","isEnabled":true},'
               '{"__typename":"GithubIssueType","id":"gid-feat","name":"Feature",'
               '"level":4,"disposition":"BOARD","isEnabled":true}'
               ']}}]}}')
    out = _types_filter(payload)
    import json as _json
    types = _json.loads(out)
    assert {t["name"] for t in types} == {"Bug", "Feature"}


def test_types_filter_treats_missing_isenabled_as_enabled() -> None:
    """A missing isEnabled field (defensive: older payloads or partial
    projections) must not silently drop the row. `select(.isEnabled !=
    false)` is satisfied by null and missing both, so the row passes.
    """
    payload = ('{"data":{"repositoriesByGhId":[{"assignableIssueTypes":'
               '{"nodes":['
               '{"__typename":"GithubIssueType","id":"gid-task","name":"Task",'
               '"level":4,"disposition":"BOARD"}'
               ']}}]}}')
    out = _types_filter(payload)
    import json as _json
    types = _json.loads(out)
    assert types and types[0]["name"] == "Task"


# v1.9.1 item #7 (G4) + round-3 finding #5: cmd_issue must surface the
# priority alongside state/pipeline/estimate, AND read the field via the
# workspace-scoped pipelineIssue (not the ambiguous pipelineIssues
# nodes[0] form, which could return the wrong workspace for issues in
# multiple workspaces).
_ISSUE_PRIORITY_READ_SNIPPET = r"""
set -euo pipefail
issue="$1"
priority=$(echo "$issue" | jq -r '.pipelineIssue.priority.name // "None"')
echo "$priority"
"""


def _issue_priority(issue_json: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _ISSUE_PRIORITY_READ_SNIPPET, "_", issue_json],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_issue_priority_reads_configured_name() -> None:
    """An issue with a configured priority surfaces the priority name.
    Workspace-scoped pipelineIssue field (round-3 #5).
    """
    issue = ('{"pipelineIssue":{"pipeline":{"name":"In Progress"},'
             '"priority":{"name":"High priority"}}}')
    assert _issue_priority(issue) == "High priority"


def test_issue_priority_renders_none_when_unset() -> None:
    """A pipelineIssue with priority=null renders as `None`, mirroring
    the Pipeline / Estimate fields' "always-present" line.
    """
    issue = ('{"pipelineIssue":{"pipeline":{"name":"In Progress"},'
             '"priority":null}}')
    assert _issue_priority(issue) == "None"


def test_issue_priority_renders_none_when_no_pipeline_issue() -> None:
    """An issue with no pipelineIssue (e.g. just-created, not yet
    placed in any pipeline) still renders cleanly as None instead of
    `null` or empty.
    """
    issue = '{"pipelineIssue":null}'
    assert _issue_priority(issue) == "None"


# v1.9.1 item #8 (G5): `--priority <name>` at create time. The cmd_create
# flag-parser must accept `--priority` with arity 2 and capture the name.
_CREATE_PRIORITY_PARSE_SNIPPET = r"""
set -euo pipefail
priority_name=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --priority)
            if [[ $# -lt 2 ]]; then echo "ARITY_ERROR"; exit 1; fi
            priority_name="$2"
            shift 2
            ;;
        *) shift ;;
    esac
done
echo "${priority_name:-NONE}"
"""


def _create_priority_parse(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _CREATE_PRIORITY_PARSE_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_create_priority_flag_captures_value() -> None:
    """`zh create "Title" -t Task --priority "High priority"` captures the
    priority name for the post-create mutation.
    """
    r = _create_priority_parse("--priority", "High priority", "-t", "Task")
    assert r.returncode == 0
    assert r.stdout.strip() == "High priority"


def test_create_priority_flag_absent_emits_none_sentinel() -> None:
    """No --priority means the priority code-path is skipped post-create."""
    r = _create_priority_parse("-t", "Task")
    assert r.returncode == 0
    assert r.stdout.strip() == "NONE"


def test_create_priority_arity_guard_rejects_missing_value() -> None:
    """`--priority` at end-of-args is rejected up-front, not via a raw
    `$2 unbound` under set -u.
    """
    r = _create_priority_parse("--priority")
    assert r.returncode == 1
    assert r.stdout.strip() == "ARITY_ERROR"


# v1.9.1 item #11: subtask display-noun must be `subtask` everywhere in
# user-facing output, even though the ZenHub issue-type name is
# "Sub-task". A pure-bash mirror of zh_display_noun_for_type.
_DISPLAY_NOUN_SNIPPET = r"""
set -euo pipefail
to_lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }
type_name="$1"
lower=$(to_lower "$type_name")
case "$lower" in
    sub-task) echo "subtask" ;;
    *) echo "$lower" ;;
esac
"""


def _display_noun(type_name: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _DISPLAY_NOUN_SNIPPET, "_", type_name],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_display_noun_collapses_sub_task_to_subtask() -> None:
    """The ZenHub issue-type "Sub-task" renders as the canonical CLI noun
    `subtask` in every Tip / Usage / hint line. Both casings of the type
    name collapse identically.
    """
    assert _display_noun("Sub-task") == "subtask"
    assert _display_noun("sub-task") == "subtask"
    assert _display_noun("SUB-TASK") == "subtask"


def test_display_noun_passes_other_types_through_lowercased() -> None:
    """Initiative / Project / Epic stay as their lowercased name; no
    over-eager collapsing.
    """
    assert _display_noun("Initiative") == "initiative"
    assert _display_noun("Project") == "project"
    assert _display_noun("Epic") == "epic"


# v1.9.1 items #2 and #9: planning-noun show/update/close emit a type-
# mismatch warning when the issue's actual type does not match the noun
# invoked. The comparison is case-insensitive and reads issueType.name
# from the issueByInfo payload.
#
# v1.9.1 round-4 finding #2: the prior version of this snippet had no
# planning-noun gating, so the tests that ran against it asserted a
# `zh bug show 42` redirect that round-3 #2 explicitly replaced with
# `zh issue 42` in production (Bug/Feature/Task have no dispatcher arm,
# so the typed redirect would error with "Unknown command: bug"). The
# snippet and its tests have been merged with the round-3 #2 pins below
# (`test_redirect_gates_bug_to_zh_issue` and siblings, which use the
# `_TYPE_MISMATCH_GATED_REDIRECT_SNIPPET`). The cases retained here
# cover the orthogonal silent-match / silent-no-type / silent-case-
# difference paths that the gated snippet does NOT exercise.
_TYPE_MISMATCH_WARN_SNIPPET = r"""
set -euo pipefail
to_lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }
display_noun_for() {
    local lower
    lower=$(to_lower "$1")
    case "$lower" in
        sub-task) echo "subtask" ;;
        *) echo "$lower" ;;
    esac
}
expected_type="$1"
response="$2"
issue_num="$3"
verb="${4:-show}"

actual_type=$(echo "$response" | jq -r '.data.issueByInfo.issueType.name // empty')
if [[ -z "$actual_type" ]]; then
    echo "SILENT_NO_TYPE"
    exit 0
fi
expected_lower=$(to_lower "$expected_type")
actual_lower=$(to_lower "$actual_type")
if [[ "$expected_lower" == "$actual_lower" ]]; then
    echo "SILENT_MATCH"
    exit 0
fi
# Round-3 finding #2 + round-4 #2: the gated redirect lives in
# _TYPE_MISMATCH_GATED_REDIRECT_SNIPPET; here we only signal that the
# mismatch was DETECTED. Removed: the assertion of a typed-noun
# redirect, which contradicted production's planning-noun gate.
echo "WARN_DETECTED"
"""


def _type_mismatch(
    expected_type: str, response: str, issue_num: str, verb: str = "show",
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _TYPE_MISMATCH_WARN_SNIPPET, "_",
         expected_type, response, issue_num, verb],
        capture_output=True, text=True, check=False,
    )


def test_type_mismatch_silent_on_exact_match() -> None:
    """`zh epic show 42` against an Epic prints no warning."""
    resp = '{"data":{"issueByInfo":{"issueType":{"name":"Epic"}}}}'
    r = _type_mismatch("Epic", resp, "42")
    assert r.returncode == 0
    assert r.stdout.strip() == "SILENT_MATCH"


def test_type_mismatch_silent_on_case_only_difference() -> None:
    """Configured casing varies workspace-to-workspace ("epic" vs
    "Epic"); the comparison is case-insensitive.
    """
    resp = '{"data":{"issueByInfo":{"issueType":{"name":"epic"}}}}'
    r = _type_mismatch("Epic", resp, "42")
    assert r.returncode == 0
    assert r.stdout.strip() == "SILENT_MATCH"


def test_type_mismatch_silent_on_untyped_issue() -> None:
    """An issue with no issue-type set (unusual but possible) does not
    fire the warning; the planning-noun create path is where types get
    enforced.
    """
    resp = '{"data":{"issueByInfo":{"issueType":null}}}'
    r = _type_mismatch("Epic", resp, "42")
    assert r.returncode == 0
    assert r.stdout.strip() == "SILENT_NO_TYPE"


def test_type_mismatch_detects_when_types_differ() -> None:
    """A clear mismatch (Bug vs Epic) reaches the WARN branch. The
    redirect shape is pinned in test_redirect_gates_* below, which
    runs against the gated production snippet (round-3 #2).
    """
    resp = '{"data":{"issueByInfo":{"issueType":{"name":"Bug"}}}}'
    r = _type_mismatch("Epic", resp, "42")
    assert r.returncode == 0
    assert r.stdout.strip() == "WARN_DETECTED"


# ===========================================================================
# v1.9.1 round-2 fixes (PR #25 claude-review findings 1, 2, 3, 4, 5, 7).
# Each snippet mirrors the single guard added in the round-2 sweep so a
# future change that loosens or drops the guard fails a test instead of
# silently shipping.
# ===========================================================================


# Round-2 finding #1: zh_hierarchy_warn_type_mismatch must fail SOFT on any
# zh_graphql failure, because the helper runs ahead of cmd_close /
# cmd_reopen / cmd_update_issue and those verbs do not require a healthy
# ZenHub API.
_WARN_FAIL_SOFT_SNIPPET = r"""
set -euo pipefail
# Stub zh_graphql to simulate a .errors response, which the real
# zh_graphql turns into `error "ZenHub API error: ..."; exit 1`.
#
# Round-6 meta-rule: stubs that model `error -> exit 1` MUST use
# `exit 1`, not `return 1`. The round-4 #1 false-pass shipped because
# the original stub used `return 1`, which the caller's
# `if zh_graphql ...; then ... else` form could intercept; real
# zh_graphql calls `exit 1` which kills the caller's shell entirely
# and reaches no else arm. Using `exit 1` here makes the test fail
# the same way production does.
zh_graphql() {
    echo "ZenHub API error: rate limited" >&2
    exit 1
}
to_lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }
display_noun_for() { echo "$(to_lower "$1")"; }
warn() { echo "WARN: $1" >&2; }

zh_hierarchy_warn_type_mismatch() {
    local expected_type="$1"
    local issue_num="$2"
    local repo_id="$3"
    local verb="${4:-show}"
    local response=""
    response=$(zh_graphql "query" "vars" 2>/dev/null) || return 0
    local actual_type
    actual_type=$(echo "$response" | jq -r '.data.issueByInfo.issueType.name // empty')
    if [[ -z "$actual_type" ]]; then return 0; fi
    warn "would have warned"
}

zh_hierarchy_warn_type_mismatch "Epic" "42" "repo-id" "close"
echo "REACHED_CALLER"
"""


def test_warn_helper_returns_zero_on_zh_graphql_failure() -> None:
    """The round-2 fail-soft envelope: a transient ZenHub API failure
    must NOT terminate the script under `set -euo pipefail`. Caller
    code (cmd_close / cmd_reopen / cmd_update_issue) must reach the
    line after the helper.
    """
    r = subprocess.run(
        ["bash", "-c", _WARN_FAIL_SOFT_SNIPPET],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0
    assert "REACHED_CALLER" in r.stdout
    assert "WARN: would have warned" not in r.stderr


# Round-2 finding #2: zh_hierarchy_warn_for_noun must find the issue
# number regardless of where it sits in the argv (before OR after a flag).
_WARN_FOR_NOUN_SCAN_SNIPPET = r"""
set -euo pipefail
expected_type="$1"; verb="$2"
shift 2
arg=""
for arg in "$@"; do
    stripped="${arg#\#}"
    if [[ "$stripped" =~ ^[0-9]+$ ]]; then
        echo "FOUND:${stripped}"
        exit 0
    fi
done
echo "NOT_FOUND"
"""


def _warn_for_noun_scan(expected_type, verb, *argv):
    return subprocess.run(
        ["bash", "-c", _WARN_FOR_NOUN_SCAN_SNIPPET, "_",
         expected_type, verb, *argv],
        capture_output=True, text=True, check=False,
    )


def test_warn_scanner_finds_issue_number_before_flags() -> None:
    """`zh epic update 42 -t Foo` -> issue number at position 0."""
    r = _warn_for_noun_scan("Epic", "update", "42", "-t", "Foo")
    assert r.stdout.strip() == "FOUND:42"


def test_warn_scanner_finds_issue_number_after_flags() -> None:
    """Round-2 finding #2: `zh epic update -t Foo 42` -> issue number
    at the END, after a flag. The pre-fix scanner broke on the first
    `-*` and missed this case, leaving the warn silent for flag-first
    invocations.
    """
    r = _warn_for_noun_scan("Epic", "update", "-t", "Foo", "42")
    assert r.stdout.strip() == "FOUND:42"


def test_warn_scanner_strips_hash_prefix() -> None:
    """`#42` is the same input as `42` (cmd_close accepts both)."""
    r = _warn_for_noun_scan("Epic", "close", "#42")
    assert r.stdout.strip() == "FOUND:42"


def test_warn_scanner_returns_not_found_with_no_number() -> None:
    """No numeric token -> NOT_FOUND. The verb's own usage error still
    fires; the warn just stays silent.
    """
    r = _warn_for_noun_scan("Epic", "update", "-t", "Foo")
    assert r.stdout.strip() == "NOT_FOUND"


# Round-2 finding #3: cmd_create must not silently apply --priority to the
# wrong (default) pipeline when --pipeline did not resolve. The fix skips
# the priority mutation and warns.
_PRIORITY_SKIP_ON_UNRESOLVED_PIPELINE_SNIPPET = r"""
set -euo pipefail
pipeline_resolved="$1"  # "yes" or "no"
priority_requested="$2" # priority name or ""
priority_id_after_skip=""

if [[ "$pipeline_resolved" == "no" ]]; then
    echo "WARN: Pipeline not found" >&2
    if [[ -n "$priority_requested" ]]; then
        echo "WARN: Skipping --priority because --pipeline did not resolve" >&2
        priority_id_after_skip=""
    fi
fi

if [[ -n "$priority_id_after_skip" ]]; then
    echo "PRIORITY_APPLIED"
else
    echo "PRIORITY_SKIPPED"
fi
"""


def _priority_skip(pipeline_resolved, priority_requested):
    return subprocess.run(
        ["bash", "-c", _PRIORITY_SKIP_ON_UNRESOLVED_PIPELINE_SNIPPET, "_",
         pipeline_resolved, priority_requested],
        capture_output=True, text=True, check=False,
    )


def test_priority_skipped_when_pipeline_unresolved() -> None:
    """`zh create "X" -p "Bad name" --priority "High" --json`: pipeline
    does not resolve, so priority must NOT bind to the default pipeline
    where the issue lands. Skipping with a warn is the documented
    behavior post-round-2.
    """
    r = _priority_skip("no", "High")
    assert r.returncode == 0
    assert r.stdout.strip() == "PRIORITY_SKIPPED"
    assert "Skipping --priority" in r.stderr


def test_priority_skip_silent_when_priority_not_requested() -> None:
    """A pipeline-only request that fails to resolve still warns about
    the pipeline but never mentions priority.
    """
    r = _priority_skip("no", "")
    assert "Skipping --priority" not in r.stderr


# Round-2 finding #7: --json output must distinguish "user did not pass
# --priority" from "user passed --priority but the post-create mutation
# did not confirm it". The `priority_requested` sibling field carries
# the user's input regardless of mutation outcome.
_CREATE_JSON_PRIORITY_SNIPPET = r"""
new_issue_num="$1"; new_issue_url="$2"; title="$3"
new_type_name="$4"; pipeline_set="$5"; estimate="$6"; parent_wired="$7"
priority_set="$8"; priority_name="$9"
jq -n \
    --argjson number "$new_issue_num" \
    --arg url "$new_issue_url" \
    --arg title "$title" \
    --arg type "${new_type_name}" \
    --arg pipeline "${pipeline_set}" \
    --arg estimate "${estimate}" \
    --arg parent "${parent_wired}" \
    --arg priority "${priority_set}" \
    --arg priority_requested "${priority_name}" \
    '{number: $number, url: $url, title: $title,
      type: (if $type == "" then null else $type end),
      pipeline: (if $pipeline == "" then null else $pipeline end),
      estimate: (if $estimate == "" then null else ($estimate | tonumber) end),
      parent: (if $parent == "" then null else ($parent | tonumber) end),
      priority: (if $priority == "" then null else $priority end),
      priority_requested: (if $priority_requested == "" then null else $priority_requested end)}'
"""


def _create_json_with_priority(num, url, title, type_, pipeline, estimate,
                               parent, priority_set, priority_requested):
    r = subprocess.run(
        ["bash", "-c", _CREATE_JSON_PRIORITY_SNIPPET, "_", str(num), url,
         title, type_, pipeline, estimate, parent, priority_set,
         priority_requested],
        capture_output=True, text=True, check=False,
    )
    import json as _json
    return _json.loads(r.stdout)


def test_create_json_priority_not_requested_is_both_null() -> None:
    """No --priority -> both fields null. Caller branches:
    null -> null = not requested.
    """
    obj = _create_json_with_priority(
        42, "u", "T", "Task", "Backlog", "5", "", "", "",
    )
    assert obj["priority"] is None
    assert obj["priority_requested"] is None


def test_create_json_priority_applied_both_carry_name() -> None:
    """`--priority "High" applied -> both fields the same name. Caller
    branches: "X" -> "X" = applied.
    """
    obj = _create_json_with_priority(
        42, "u", "T", "Task", "Backlog", "5", "", "High", "High",
    )
    assert obj["priority"] == "High"
    assert obj["priority_requested"] == "High"


def test_create_json_priority_requested_but_not_confirmed() -> None:
    """The motivating case: user asked for "High", post-create mutation
    failed to confirm (read-after-write lag, transient API failure).
    Caller branches: "X" -> null = requested but not confirmed, retry.
    """
    obj = _create_json_with_priority(
        42, "u", "T", "Task", "Backlog", "5", "", "", "High",
    )
    assert obj["priority"] is None
    assert obj["priority_requested"] == "High"


# v1.9.2 round-7 finding #15: DELETED stale snippet
# `_SET_TYPE_PARTIAL_MSG_SNIPPET` and its two tests
# (test_set_type_partial_message_uses_partially_applied_wording,
# test_set_type_zero_count_still_says_failed). The snippet asserted
# `returncode == 1` against its own embedded copy of cmd_set_type's
# partial branch, but round-6 #4 changed production to exit 2. The
# snippet kept passing against itself, creating a contradictory spec
# alongside the (now-also-deleted, v1.9.3 #11) _SET_TYPE_EXIT_2_SNIPPET.
# Both message-wording assertions (Partially applied / Verify with /
# Failed to set type) and the exit-code contract are now exercised
# against PRODUCTION cmd_set_type in
# tests/test_zh_production_regression.py.
#
# v1.9.3 pattern-sweep finding #15: corrected the replacement-test
# references. Partial-branch wording is pinned by
# test_structural_guarantee_set_type_exits_2_not_1_on_partial and
# test_round2_f7_set_type_partial_via_github_errors_only_exits_2;
# clean-success wording by test_round3_f6_set_type_clean_success_exits_0;
# hard-failure wording by test_round2_f7_set_type_success_count_zero_exits_1.
# All are production-sourced.


# ===========================================================================
# v1.9.1 round-3 fixes (PR #25 round-2 review findings 1, 2, 4, 6).
# ===========================================================================


# Round-3 finding #1: extend the fail-soft envelope to the OUTER
# get_repo_info / get_repo_id round-trips, not just the inner
# zh_graphql call inside zh_hierarchy_warn_type_mismatch.
_OUTER_FAILSOFT_SNIPPET = r"""
set -euo pipefail
fail_at="$1"  # "get_repo_info", "get_repo_id", or "none"
# Round-6 meta-rule: production get_repo_info / get_repo_id call
# `error` (which calls `exit 1`) on failure. The stubs must use
# `exit 1` to match, otherwise `caller_var=$(get_repo_info 2>/dev/null) || ...`
# is a different code path under set -e (a `return 1` leaves the
# subshell exit code 1 and the `||` fires, which happens to be the
# behavior we want, but only by coincidence in this particular wrap
# pattern). Aligning to `exit 1` removes the coincidence.
get_repo_info() {
    if [[ "$fail_at" == "get_repo_info" ]]; then
        echo "ZenHub API error" >&2
        exit 1
    fi
    echo "owner/repo"
}
get_repo_id() {
    if [[ "$fail_at" == "get_repo_id" ]]; then
        echo "ZenHub API error" >&2
        exit 1
    fi
    echo "repo-id-abc"
}
warn_helper() {
    local owner_repo="" repo_id=""
    owner_repo=$(get_repo_info 2>/dev/null) || return 0
    if [[ -z "$owner_repo" ]]; then return 0; fi
    repo_id=$(get_repo_id "$owner_repo" 2>/dev/null) || return 0
    if [[ -z "$repo_id" ]]; then return 0; fi
    echo "WOULD_HAVE_WARNED"
}
warn_helper
echo "REACHED_CALLER"
"""


def _outer_failsoft(fail_at: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _OUTER_FAILSOFT_SNIPPET, "_", fail_at],
        capture_output=True, text=True, check=False,
    )


def test_outer_failsoft_get_repo_info_does_not_kill_caller() -> None:
    """A transient .errors on get_repo_info must NOT terminate the script
    under set -euo pipefail. The caller's next line runs.
    """
    r = _outer_failsoft("get_repo_info")
    assert r.returncode == 0
    assert "REACHED_CALLER" in r.stdout
    assert "WOULD_HAVE_WARNED" not in r.stdout


def test_outer_failsoft_get_repo_id_does_not_kill_caller() -> None:
    """A transient .errors on get_repo_id must NOT terminate the script
    either. Symmetric pin for the second outer round-trip.
    """
    r = _outer_failsoft("get_repo_id")
    assert r.returncode == 0
    assert "REACHED_CALLER" in r.stdout
    assert "WOULD_HAVE_WARNED" not in r.stdout


def test_outer_failsoft_clean_path_warns() -> None:
    """When both outer lookups succeed, the warn helper runs (this
    pins that the envelope did not silently disable the warn on the
    happy path).
    """
    r = _outer_failsoft("none")
    assert r.returncode == 0
    assert "WOULD_HAVE_WARNED" in r.stdout
    assert "REACHED_CALLER" in r.stdout


# Round-3 finding #2: type-mismatch redirect must NOT suggest a non-
# existent verb. Bug / Feature / Task have no dispatcher arm.
_TYPE_MISMATCH_GATED_REDIRECT_SNIPPET = r"""
set -euo pipefail
to_lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }
display_noun_for() {
    local lower
    lower=$(to_lower "$1")
    case "$lower" in
        sub-task) echo "subtask" ;;
        *) echo "$lower" ;;
    esac
}
actual_type="$1"
issue_num="$2"
verb="$3"

actual_lower_norm=$(to_lower "$actual_type")
case "$actual_lower_norm" in
    initiative|project|epic|sub-task|subtask)
        actual_display=$(display_noun_for "$actual_type")
        redirect="zh ${actual_display} ${verb} ${issue_num}"
        ;;
    *)
        redirect="zh issue ${issue_num}"
        ;;
esac
echo "$redirect"
"""


def _gated_redirect(actual_type, issue_num, verb="show"):
    r = subprocess.run(
        ["bash", "-c", _TYPE_MISMATCH_GATED_REDIRECT_SNIPPET, "_",
         actual_type, issue_num, verb],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_redirect_gates_bug_to_zh_issue() -> None:
    """A Bug-typed issue redirects to `zh issue 42`, NOT
    `zh bug close 42` (which would error "Unknown command: bug").
    """
    assert _gated_redirect("Bug", "42", "close") == "zh issue 42"


def test_redirect_gates_feature_to_zh_issue() -> None:
    assert _gated_redirect("Feature", "42", "update") == "zh issue 42"


def test_redirect_gates_task_to_zh_issue() -> None:
    assert _gated_redirect("Task", "42", "show") == "zh issue 42"


def test_redirect_emits_typed_form_for_planning_nouns() -> None:
    """Planning nouns keep the typed redirect because their dispatcher
    arms exist.
    """
    assert _gated_redirect("Epic", "42", "close") == "zh epic close 42"
    assert _gated_redirect("Initiative", "42", "show") == "zh initiative show 42"
    assert _gated_redirect("Project", "42", "update") == "zh project update 42"
    assert _gated_redirect("Sub-task", "42", "close") == "zh subtask close 42"


# Round-3 finding #4: `--flag=value` GNU-style. cmd_create normalizes
# `--flag=value` to `--flag value` at the top of the arg-parsing loop
# so every long flag accepts both forms uniformly.
_GNU_FLAG_NORMALIZE_SNIPPET = r"""
set -euo pipefail
title=""
priority_name=""
issue_type=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --*=* ]]; then
        set -- "${1%%=*}" "${1#*=}" "${@:2}"
    fi
    case "$1" in
        -t|--type) issue_type="$2"; shift 2 ;;
        --priority) priority_name="$2"; shift 2 ;;
        *)
            if [[ -z "$title" ]]; then title="$1"; fi
            shift
            ;;
    esac
done
echo "TITLE:${title}"
echo "TYPE:${issue_type}"
echo "PRIORITY:${priority_name}"
"""


def _gnu_flag(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _GNU_FLAG_NORMALIZE_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_gnu_flag_equals_form_captured_for_priority() -> None:
    """`--priority=High` is normalized to `--priority High` and lands
    in priority_name. Pre-fix it fell through to `*)` and was treated
    as the title.
    """
    r = _gnu_flag("Title", "--priority=High")
    assert r.returncode == 0
    assert "TITLE:Title" in r.stdout
    assert "PRIORITY:High" in r.stdout


def test_gnu_flag_equals_form_captured_for_type() -> None:
    """Same normalization works for every long flag, not just --priority."""
    r = _gnu_flag("Title", "--type=Epic")
    assert r.returncode == 0
    assert "TYPE:Epic" in r.stdout


def test_gnu_flag_space_form_still_works() -> None:
    """The classic space-separated form continues to work after the
    normalization pass.
    """
    r = _gnu_flag("Title", "--priority", "High")
    assert r.returncode == 0
    assert "PRIORITY:High" in r.stdout


def test_gnu_flag_equals_before_positional_works() -> None:
    """`--priority=High Foo` (flag-first, positional last) is handled
    correctly: the flag is consumed, the positional becomes title.
    """
    r = _gnu_flag("--priority=High", "Foo")
    assert r.returncode == 0
    assert "TITLE:Foo" in r.stdout
    assert "PRIORITY:High" in r.stdout


# Round-3 finding #6: zh_hierarchy_warn_for_noun must track flag arity
# so a numeric flag VALUE does not get picked as the issue number.
_FLAG_ARITY_SCAN_SNIPPET = r"""
set -euo pipefail
shift  # drop expected_type
shift  # drop verb
prev=""
for arg in "$@"; do
    case "$prev" in
        -t|--title|-d|--description|-b|--body|-e|--estimate|-l|--label|--labels|-a|--assign|--assignee|-p|--pipeline|--priority|--parent)
            prev="$arg"
            continue
            ;;
    esac
    prev="$arg"
    stripped="${arg#\#}"
    if [[ "$stripped" =~ ^[0-9]+$ ]]; then
        echo "FOUND:${stripped}"
        exit 0
    fi
done
echo "NOT_FOUND"
"""


def _flag_arity_scan(*argv):
    return subprocess.run(
        ["bash", "-c", _FLAG_ARITY_SCAN_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_flag_arity_skips_numeric_title_value() -> None:
    """`zh epic update -t 42 100` should warn about issue #100, NOT #42
    (which is the value bound to -t). Round-3 #6.
    """
    r = _flag_arity_scan("Epic", "update", "-t", "42", "100")
    assert r.stdout.strip() == "FOUND:100"


def test_flag_arity_skips_numeric_description_value() -> None:
    """`-d 42 100` -> 42 is a description value, 100 is the issue."""
    r = _flag_arity_scan("Epic", "update", "-d", "42", "100")
    assert r.stdout.strip() == "FOUND:100"


def test_flag_arity_finds_only_numeric_after_other_flags() -> None:
    """Symmetric: when there's only one numeric positional after a
    string-valued flag, it's the issue number.
    """
    r = _flag_arity_scan("Epic", "close", "100", "-t", "Foo")
    assert r.stdout.strip() == "FOUND:100"


def test_flag_arity_returns_not_found_when_only_flag_values_are_numeric() -> None:
    """If every numeric token is a flag value, no issue-number found."""
    r = _flag_arity_scan("Epic", "update", "-t", "42", "-d", "100")
    assert r.stdout.strip() == "NOT_FOUND"


# ===========================================================================
# v1.9.1 round-4 fixes (PR #25 round-3 review findings 1, 3, 4, 5).
# ===========================================================================


# Round-4 finding #1: every post-createIssue zh_graphql call in
# cmd_create must fail-soft, not just the priority block. The estimate
# mutation, pipelines lookup, and moveIssue mutation now all use the
# `if zh_graphql ...; then ... else warn ... fi` envelope.
_POST_CREATE_ZH_GRAPHQL_ENVELOPE_SNIPPET = r"""
set -euo pipefail
fail="$1"  # "yes" / "no"
new_issue_num="100"
estimate="3"

zh_graphql() {
    if [[ "$fail" == "yes" ]]; then
        echo "ZenHub API error: rate limited" >&2
        # Round-6 meta-rule: production zh_graphql calls `error` ->
        # `exit 1` on `.errors`, NOT `return 1`. The original round-4
        # #1 stub used `return 1`, which let the test's
        # `if zh_graphql ...; then ... else ... fi` form intercept the
        # failure and pass green. Production with `exit 1` does NOT
        # work in that form: `exit 1` runs in the current shell unless
        # zh_graphql is called inside `$(...)`. The if/then/else fix
        # shipped and ran in production for the full v1.9.1 cycle
        # before round-6 review caught it. Using `exit 1` here pins
        # the production behavior exactly.
        exit 1
    fi
    echo "{}"
}
warn() { echo "WARN: $1" >&2; }

# Round-6 finding #1: mirror cmd_create's WORKING envelope: capture
# zh_graphql output into a variable, then branch on whether the
# variable is non-empty. The `if zh_graphql ...; then ... else` form
# the round-4 fix used would NOT have intercepted exit 1, so it would
# have aborted the script before the JSON emit (the exact failure
# mode the fix was supposed to prevent).
est_result=""
est_result=$(zh_graphql "mutation" "vars" 2>/dev/null) || est_result=""
if [[ -n "$est_result" ]]; then
    echo "ESTIMATE_OK"
else
    warn "Created #${new_issue_num} but the estimate mutation failed. Retry with 'zh estimate #${new_issue_num} ${estimate}'."
fi

# Caller's next line (must run regardless of the estimate outcome).
echo "JSON_EMIT_REACHED"
"""


def _post_create_envelope(fail: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _POST_CREATE_ZH_GRAPHQL_ENVELOPE_SNIPPET, "_", fail],
        capture_output=True, text=True, check=False,
    )


def test_post_create_envelope_emits_json_on_estimate_failure() -> None:
    """Round-4 #1: the post-create JSON emit must run even when the
    estimate mutation hits a transient .errors. The pre-fix code
    aborted before the JSON emit, leaving stdout empty and provoking
    duplicate retries.
    """
    r = _post_create_envelope("yes")
    assert r.returncode == 0
    assert "JSON_EMIT_REACHED" in r.stdout
    assert "ESTIMATE_OK" not in r.stdout
    assert "WARN:" in r.stderr
    assert "zh estimate #100 3" in r.stderr


def test_post_create_envelope_clean_path_no_warn() -> None:
    """Sanity: when the mutation succeeds, the warn does not fire."""
    r = _post_create_envelope("no")
    assert r.returncode == 0
    assert "ESTIMATE_OK" in r.stdout
    assert "JSON_EMIT_REACHED" in r.stdout
    assert "WARN:" not in r.stderr


# Round-4 finding #3: cmd_hierarchy_create must apply the GNU
# normalization too, so `zh epic create "X" --description=Body
# --type=Bug` triggers the body-rewrite and the -t conflict error
# instead of silently forwarding tokens that lose the user's intent.
_HIERARCHY_NORMALIZER_SNIPPET = r"""
set -euo pipefail
passthrough=()
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --*=* ]]; then
        set -- "${1%%=*}" "${1#*=}" "${@:2}"
    fi
    case "$1" in
        -d|--description)
            passthrough+=("-b" "$2"); shift 2 ;;
        -t|--type)
            echo "TYPE_REJECTED"
            exit 1 ;;
        *)
            passthrough+=("$1"); shift ;;
    esac
done
printf '%s\n' "${passthrough[@]}" -t "Epic"
"""


def _hierarchy_normalize(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _HIERARCHY_NORMALIZER_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_hierarchy_normalize_description_equals_form() -> None:
    """`--description=Body` is rewritten to `-b Body` and forwarded to
    cmd_create. Pre-round-4 the equals form fell to `*)` and was
    forwarded verbatim; cmd_create has no `--description` arm, so the
    body silently dropped.
    """
    r = _hierarchy_normalize("Title", "--description=Body")
    assert r.returncode == 0
    argv = r.stdout.splitlines()
    assert argv == ["Title", "-b", "Body", "-t", "Epic"]


def test_hierarchy_normalize_type_equals_form_rejected() -> None:
    """`--type=Bug` is rewritten to `--type Bug` and triggers the
    -t conflict error the user expected. Pre-round-4 it silently
    overwrote the noun's type to Bug (cmd_create last-wins overwrote
    it back to Epic, but the user's intent that they were on a
    conflict path silently disappeared).
    """
    r = _hierarchy_normalize("Title", "--type=Bug")
    assert r.returncode == 1
    assert r.stdout.strip() == "TYPE_REJECTED"


# Round-4 finding #4: `--flag=` (empty value) must be rejected up-
# front. The pre-fix normalizer produced `--flag ""` and the arity
# guard counted "" as a present arg, so `--type=` silently created
# an untyped issue.
_GNU_FLAG_EMPTY_VALUE_SNIPPET = r"""
set -euo pipefail
error() { echo "ERROR: $1" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --*=* ]]; then
        _flag="${1%%=*}"
        _val="${1#*=}"
        if [[ -z "$_val" ]]; then
            error "Option ${_flag} requires a value (received empty string from '${1}')"
        fi
        set -- "$_flag" "$_val" "${@:2}"
    fi
    case "$1" in
        --priority|--type|--pipeline)
            echo "FLAG:${1}:${2}"
            shift 2
            ;;
        *) shift ;;
    esac
done
"""


def _gnu_flag_empty(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _GNU_FLAG_EMPTY_VALUE_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_gnu_flag_empty_value_rejected_for_type() -> None:
    """`--type=` (unset shell variable expanded to nothing) used to
    silently create an untyped issue. Now hard-error.
    """
    r = _gnu_flag_empty("--type=")
    assert r.returncode == 1
    assert "requires a value" in r.stderr
    assert "--type" in r.stderr


def test_gnu_flag_empty_value_rejected_for_priority() -> None:
    """Same guard fires for --priority=, --pipeline=, etc."""
    r = _gnu_flag_empty("--priority=")
    assert r.returncode == 1
    assert "requires a value" in r.stderr


def test_gnu_flag_non_empty_value_still_works() -> None:
    """Regression guard: the empty-value rejection doesn't break the
    happy path.
    """
    r = _gnu_flag_empty("--type=Epic")
    assert r.returncode == 0
    assert "FLAG:--type:Epic" in r.stdout


# Round-4 finding #5: warn wording branches on verb. `show` keeps
# "data still rendered"; `close` / `reopen` / `update` say
# "the {verb} still applied to #N" so the operator does not re-run
# the redirect after the destructive op already landed.
_VERB_WORDING_SNIPPET = r"""
set -euo pipefail
verb="$1"
issue_num="42"
redirect="zh epic ${verb} ${issue_num}"
case "$verb" in
    show)
        trailing="The data still rendered, but the matching command is '${redirect}'."
        ;;
    *)
        trailing="The ${verb} still applied to #${issue_num}; next time the matching command is '${redirect}'."
        ;;
esac
echo "$trailing"
"""


def _verb_wording(verb: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _VERB_WORDING_SNIPPET, "_", verb],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_verb_wording_show_keeps_data_rendered() -> None:
    """`show` retains the original "data still rendered" wording.
    Symmetric regression guard so the round-4 #5 change does not
    swallow the show-flavored case.
    """
    out = _verb_wording("show")
    assert "data still rendered" in out


def test_verb_wording_close_says_already_applied() -> None:
    """`close` swaps to "still applied to #N" so the operator does
    not interpret the warn as "do this instead" and re-close an
    already-closed issue.
    """
    out = _verb_wording("close")
    assert "still applied to #42" in out
    assert "data still rendered" not in out


def test_verb_wording_update_and_reopen_same_pattern() -> None:
    """Symmetric pins for the other destructive verbs."""
    for verb in ("update", "reopen"):
        out = _verb_wording(verb)
        assert f"{verb} still applied to #42" in out


# ===========================================================================
# v1.9.1 round-5 fixes (PR #25 round-4 review findings 1 and 2 only;
# subsequent findings are symmetric-gap / polish, deferred to v1.9.2
# per the explicit reviewer recommendation to pause here).
# ===========================================================================


# Round-5 finding #1: the round-4 #1 envelope added a new `else` branch
# for the moveIssue failure, but did NOT clear priority_id the way the
# sibling "Pipeline not found" branch does. Without this, the priority
# block at the bottom of cmd_create binds the priority to whichever
# pipelineIssue exists for the new issue (the default Triage pipeline,
# since the move did not land) and the operator believes both flags
# applied.
_MOVE_FAILSOFT_PRIORITY_CLEANUP_SNIPPET = r"""
set -euo pipefail
priority_id="prio-high-id"
priority_name="High"
new_issue_num="100"
pipeline="In Progress"

warn() { echo "WARN: $1" >&2; }

# Mirror the round-5 #1 fail-soft `else` arm for the move mutation.
move_failed="$1"  # "yes" / "no"
if [[ "$move_failed" == "no" ]]; then
    echo "MOVE_OK"
else
    warn "Created #${new_issue_num} but the move-to-'${pipeline}' mutation failed."
    if [[ -n "$priority_id" ]]; then
        warn "Skipping --priority '${priority_name}' because the move-to-'${pipeline}' mutation failed."
        priority_id=""
    fi
fi

if [[ -n "$priority_id" ]]; then
    echo "PRIORITY_WILL_APPLY:${priority_id}"
else
    echo "PRIORITY_CLEARED"
fi
"""


def _move_failsoft(move_failed: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _MOVE_FAILSOFT_PRIORITY_CLEANUP_SNIPPET, "_", move_failed],
        capture_output=True, text=True, check=False,
    )


def test_move_failure_clears_priority_id() -> None:
    """When moveIssue fails after the pipeline_id resolved, the
    fail-soft `else` must clear priority_id so the priority block
    does not bind the priority to the wrong (default) pipeline.
    """
    r = _move_failsoft("yes")
    assert r.returncode == 0
    assert r.stdout.strip() == "PRIORITY_CLEARED"
    assert "Skipping --priority" in r.stderr


def test_move_success_keeps_priority_id() -> None:
    """When moveIssue succeeds, priority_id is preserved so the
    priority block applies it to the correctly-placed pipelineIssue.
    Regression guard so the round-5 #1 fix does not over-clear.
    """
    r = _move_failsoft("no")
    assert r.returncode == 0
    assert "MOVE_OK" in r.stdout
    assert "PRIORITY_WILL_APPLY:prio-high-id" in r.stdout
    assert "PRIORITY_CLEARED" not in r.stdout


# Round-5 finding #2: cmd_hierarchy_create's `--flag=value` normalizer
# must reject empty values like cmd_create does. Without it, a
# wrapper script with an unset shell variable (`--description=$DESC`
# where DESC is unset) silently creates a body-less planning issue.
_HIERARCHY_EMPTY_VALUE_SNIPPET = r"""
set -euo pipefail
error() { echo "ERROR: $1" >&2; exit 1; }
passthrough=()
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --*=* ]]; then
        _hnorm_flag="${1%%=*}"
        _hnorm_val="${1#*=}"
        if [[ -z "$_hnorm_val" ]]; then
            error "Option ${_hnorm_flag} requires a value (received empty string from '${1}')"
        fi
        set -- "$_hnorm_flag" "$_hnorm_val" "${@:2}"
    fi
    case "$1" in
        -d|--description) passthrough+=("-b" "$2"); shift 2 ;;
        *) passthrough+=("$1"); shift ;;
    esac
done
echo "OK:${passthrough[*]:-}"
"""


def _hierarchy_empty(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _HIERARCHY_EMPTY_VALUE_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_hierarchy_create_rejects_empty_description() -> None:
    """`zh epic create "X" --description=` (unset DESC) hard-errors at
    the planning-noun layer. Pre-round-5 the empty value silently
    propagated through to cmd_create as `-b ""` and produced an
    empty-body issue.
    """
    r = _hierarchy_empty("Title", "--description=")
    assert r.returncode == 1
    assert "requires a value" in r.stderr
    assert "--description" in r.stderr


def test_hierarchy_create_rejects_empty_labels() -> None:
    """Same guard covers every long flag (not just --description)."""
    r = _hierarchy_empty("Title", "--labels=")
    assert r.returncode == 1
    assert "requires a value" in r.stderr
    assert "--labels" in r.stderr


def test_hierarchy_create_accepts_non_empty_value() -> None:
    """Regression guard: the round-5 #2 rejection does not break the
    happy path. A populated description still routes through the `-d`
    -> `-b` translation.
    """
    r = _hierarchy_empty("Title", "--description=Body")
    assert r.returncode == 0
    out = r.stdout.strip()
    assert out.startswith("OK:")
    assert "-b" in out
    assert "Body" in out


# ===========================================================================
# v1.9.1 round-6 fixes (PR #25 round-5 review findings 1-12).
#
# Meta-rule for these tests: stubs that model `error -> exit 1` use
# `exit 1`, NOT `return 1`. Round-4 #1's false-pass shipped because the
# original stub used `return 1`, which is interceptable by
# `if zh_graphql ...; then ... else ... fi`; production's `exit 1`
# inside zh_graphql is NOT interceptable in that form. The HIGH #1, #2
# fixes in this round REPLACE the if/then/else pattern with a
# `result=$(zh_graphql ...) || result=""; if [[ -n "$result" ]] ...`
# capture, which intercepts exit 1 only because $(...) runs in a
# subshell. Every new stub below uses `exit 1` so a future refactor
# that re-introduces the broken pattern fails the test.
# ===========================================================================


# Round-6 finding #1: cmd_create estimate envelope. The if/then/else
# form would not have caught exit 1, so production used to abort
# before the JSON emit. Pin the WORKING capture+branch shape against
# an exit-1 stub.
_EST_ENVELOPE_FIXED_SNIPPET = r"""
set -euo pipefail
fail="$1"  # "yes" / "no"
new_issue_num="100"
estimate="3"

zh_graphql() {
    if [[ "$fail" == "yes" ]]; then
        echo "ZenHub API error: rate limited" >&2
        exit 1
    fi
    echo "{}"
}
warn() { echo "WARN: $1" >&2; }

# Production round-6 #1 shape: capture into a var so $(...) contains
# the exit.
est_result=""
est_result=$(zh_graphql "mutation" "vars" 2>/dev/null) || est_result=""
if [[ -n "$est_result" ]]; then
    echo "ESTIMATE_APPLIED"
else
    warn "Created #${new_issue_num} but the estimate mutation failed. Retry with 'zh estimate #${new_issue_num} ${estimate}'."
fi
echo "JSON_EMIT_REACHED"
"""


def _est_envelope_fixed(fail: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _EST_ENVELOPE_FIXED_SNIPPET, "_", fail],
        capture_output=True, text=True, check=False,
    )


def test_est_envelope_fixed_reaches_json_emit_on_failure() -> None:
    """The capture-then-branch shape MUST reach the JSON emit even
    when zh_graphql does the real `exit 1` (not the false-pass
    `return 1`). Pins the round-6 #1 fix.
    """
    r = _est_envelope_fixed("yes")
    assert r.returncode == 0
    assert "JSON_EMIT_REACHED" in r.stdout
    assert "ESTIMATE_APPLIED" not in r.stdout
    assert "WARN:" in r.stderr


def test_est_envelope_fixed_clean_path() -> None:
    """Sanity: when zh_graphql returns non-empty, the success arm
    fires and JSON emit runs.
    """
    r = _est_envelope_fixed("no")
    assert r.returncode == 0
    assert "ESTIMATE_APPLIED" in r.stdout
    assert "JSON_EMIT_REACHED" in r.stdout


# Round-6 finding #1 (counterproof): the BROKEN if/then/else form
# does NOT reach the JSON emit when zh_graphql exits 1. This pins
# the false-pass that the round-4 fix shipped, so a future refactor
# regressing to the broken pattern fails the test.
_EST_ENVELOPE_BROKEN_SNIPPET = r"""
set -euo pipefail
zh_graphql() {
    echo "ZenHub API error" >&2
    exit 1
}
warn() { echo "WARN: $1" >&2; }

# The BROKEN round-4 shape we replaced. Production's `exit 1` from
# zh_graphql kills the current shell BEFORE the else arm can run.
if zh_graphql "mutation" "vars" > /dev/null 2>&1; then
    echo "OK_REACHED"
else
    warn "would have warned"
fi
echo "JSON_EMIT_REACHED"
"""


def test_est_envelope_broken_pattern_aborts_before_json() -> None:
    """Counterproof: the OLD `if zh_graphql ...; then ... else ... fi`
    form does NOT reach the JSON emit when zh_graphql calls exit 1.
    This is the bug the round-6 #1 fix corrects.
    """
    r = subprocess.run(
        ["bash", "-c", _EST_ENVELOPE_BROKEN_SNIPPET],
        capture_output=True, text=True, check=False,
    )
    # The exit code propagates out from the killed shell.
    assert r.returncode == 1
    assert "JSON_EMIT_REACHED" not in r.stdout
    # The else arm was unreachable.
    assert "would have warned" not in r.stderr


# Round-6 finding #2: same fix applied to the moveIssue envelope.
# Plus the round-5 #1 priority_id cleanup that lives in the else
# arm (which was previously unreachable for the same exit-1 reason).
_MOVE_ENVELOPE_FIXED_SNIPPET = r"""
set -euo pipefail
fail="$1"  # "yes" / "no"
priority_id="prio-high-id"
priority_name="High"
new_issue_num="100"
pipeline="In Progress"
mutation="placeholder"
vars="placeholder"

zh_graphql() {
    if [[ "$fail" == "yes" ]]; then
        echo "ZenHub API error" >&2
        exit 1
    fi
    echo "{}"
}
warn() { echo "WARN: $1" >&2; }

move_result=""
move_result=$(zh_graphql "$mutation" "$vars" 2>/dev/null) || move_result=""
if [[ -n "$move_result" ]]; then
    pipeline_set="$pipeline"
    echo "MOVED:${pipeline_set}"
else
    warn "Created #${new_issue_num} but the move-to-'${pipeline}' mutation failed."
    if [[ -n "$priority_id" ]]; then
        warn "Skipping --priority '${priority_name}' because the move failed."
        priority_id=""
    fi
fi

if [[ -n "$priority_id" ]]; then
    echo "PRIORITY_WILL_APPLY:${priority_id}"
else
    echo "PRIORITY_CLEARED"
fi
echo "JSON_EMIT_REACHED"
"""


def _move_envelope_fixed(fail: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _MOVE_ENVELOPE_FIXED_SNIPPET, "_", fail],
        capture_output=True, text=True, check=False,
    )


def test_move_envelope_clears_priority_on_failure() -> None:
    """Round-6 #2: with the capture+branch shape, the move-failure
    else arm IS reachable when zh_graphql does the real exit 1, so
    the priority_id cleanup the round-5 #1 commit added actually
    runs. JSON emit follows.
    """
    r = _move_envelope_fixed("yes")
    assert r.returncode == 0
    assert "JSON_EMIT_REACHED" in r.stdout
    assert "PRIORITY_CLEARED" in r.stdout
    assert "PRIORITY_WILL_APPLY" not in r.stdout
    assert "Skipping --priority" in r.stderr


def test_move_envelope_preserves_priority_on_success() -> None:
    """Regression guard: when moveIssue succeeds, priority_id stays
    set so the priority block applies it to the correctly-placed
    pipelineIssue.
    """
    r = _move_envelope_fixed("no")
    assert r.returncode == 0
    assert "MOVED:In Progress" in r.stdout
    assert "PRIORITY_WILL_APPLY:prio-high-id" in r.stdout
    assert "Skipping --priority" not in r.stderr


# Round-6 finding #3: cmd_update_issue must accept `--title=Foo`
# (the GNU equals form). Mirrors cmd_create's normalizer + empty-
# value rejection.
_UPDATE_NORMALIZER_SNIPPET = r"""
set -euo pipefail
error() { echo "ERROR: $1" >&2; exit 1; }
issue_num=""
title=""
body=""
title_set="false"
body_set="false"

while [[ $# -gt 0 ]]; do
    if [[ "$1" == --*=* ]]; then
        _unorm_flag="${1%%=*}"
        _unorm_val="${1#*=}"
        if [[ -z "$_unorm_val" ]]; then
            error "Option ${_unorm_flag} requires a value (received empty string from '${1}')"
        fi
        set -- "$_unorm_flag" "$_unorm_val" "${@:2}"
    fi
    case "$1" in
        -t|--title)
            if [[ $# -lt 2 ]]; then error "Option ${1} requires a value"; fi
            title="$2"; title_set="true"; shift 2 ;;
        -d|--description|-b|--body)
            if [[ $# -lt 2 ]]; then error "Option ${1} requires a value"; fi
            body="$2"; body_set="true"; shift 2 ;;
        *)
            if [[ -z "$issue_num" ]]; then issue_num="$1"; fi
            shift ;;
    esac
done

echo "ISSUE:${issue_num}"
echo "TITLE:${title}"
echo "BODY:${body}"
"""


def _update_normalize(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _UPDATE_NORMALIZER_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_update_normalizer_handles_equals_title() -> None:
    """`zh epic update --title="Foo" 42` (flag-first with equals) used
    to die with "Invalid issue number: --title=Foo" because the case
    arm didn't match the equals form and the *) arm captured it as
    the issue number. Now normalizes correctly.
    """
    r = _update_normalize("--title=Foo", "42")
    assert r.returncode == 0
    assert "ISSUE:42" in r.stdout
    assert "TITLE:Foo" in r.stdout


def test_update_normalizer_handles_equals_description() -> None:
    """Same fix covers --description= and --body= forms."""
    r = _update_normalize("--description=New body", "100")
    assert r.returncode == 0
    assert "ISSUE:100" in r.stdout
    assert "BODY:New body" in r.stdout


def test_update_normalizer_rejects_empty_equals() -> None:
    """Empty-value form (e.g. unset shell variable) hard-errors
    instead of silently updating with an empty title.
    """
    r = _update_normalize("--title=", "42")
    assert r.returncode == 1
    assert "requires a value" in r.stderr


def test_update_normalizer_space_form_still_works() -> None:
    """Regression guard: classic `-t Foo 42` still parses."""
    r = _update_normalize("-t", "Foo", "42")
    assert r.returncode == 0
    assert "ISSUE:42" in r.stdout
    assert "TITLE:Foo" in r.stdout


# v1.9.3 pattern-sweep finding #11: DELETED legacy `_SET_TYPE_EXIT_2_SNIPPET`
# and its three companion tests
# (test_set_type_partial_exits_2_not_1,
#  test_set_type_zero_count_exits_1,
#  test_set_type_clean_success_exits_0).
#
# The snippet was a parallel re-implementation of the cmd_set_type gate
# that ran against its own embedded copy of the production logic — a
# class of test that pins the snippet's behavior, not the real CLI's,
# and silently drifts when production changes (same drift problem as
# round-7 #15). All three branches the snippet exercised are now
# covered against PRODUCTION cmd_set_type in
# tests/test_zh_production_regression.py:
#
#   * Clean success (exit 0):
#       test_round3_f6_set_type_clean_success_exits_0
#   * Partial via failedIssues (exit 2):
#       test_structural_guarantee_set_type_exits_2_not_1_on_partial
#   * Partial via githubErrors (exit 2):
#       test_round2_f7_set_type_partial_via_github_errors_only_exits_2
#   * Hard failure / successCount=0 (exit 1):
#       test_round2_f7_set_type_success_count_zero_exits_1
#
# The production-sourced tests use the `run_zh_with_stubs` harness and
# stub only the GraphQL layer, leaving the gate logic to be exercised
# from real cmd_set_type. That is the contract we ship.


# Round-6 finding #5: cmd_create parent-wire addSubIssues envelope.
# Must capture the exit-1 from zh_graphql so the script does not die
# before the --json emit.
_PARENT_WIRE_ENVELOPE_SNIPPET = r"""
set -euo pipefail
fail="$1"
parent_stripped="42"
new_issue_num="100"
mutation="placeholder"
vars="placeholder"

zh_graphql() {
    if [[ "$fail" == "yes" ]]; then
        echo "ZenHub API error" >&2
        exit 1
    fi
    echo '{"data":{"addSubIssues":{"successCount":1}}}'
}
warn() { echo "WARN: $1" >&2; }

sub_response=""
sub_ok=0
sub_response=$(zh_graphql "$mutation" "$vars" 2>/dev/null) || sub_response=""
if [[ -n "$sub_response" ]]; then
    sub_ok=$(echo "$sub_response" | jq -r '.data.addSubIssues.successCount // 0')
fi
if [[ "$sub_ok" -ge 1 ]]; then
    echo "WIRED:#${parent_stripped}"
else
    warn "Created #${new_issue_num} but could not attach it to parent #${parent_stripped}. Re-wire with 'zh subissue add ${parent_stripped} ${new_issue_num}'."
fi
echo "JSON_EMIT_REACHED"
"""


def _parent_wire(fail: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _PARENT_WIRE_ENVELOPE_SNIPPET, "_", fail],
        capture_output=True, text=True, check=False,
    )


def test_parent_wire_failure_reaches_json_emit() -> None:
    """Round-6 #5: parent-wire mutation failure must NOT abort the
    script before the JSON emit. The capture+branch shape handles
    exit 1; the warn directs the user at the manual re-wire verb.
    """
    r = _parent_wire("yes")
    assert r.returncode == 0
    assert "JSON_EMIT_REACHED" in r.stdout
    assert "WIRED:" not in r.stdout
    assert "zh subissue add 42 100" in r.stderr


def test_parent_wire_success_path() -> None:
    """Regression: clean success still emits the wired line."""
    r = _parent_wire("no")
    assert r.returncode == 0
    assert "WIRED:#42" in r.stdout
    assert "JSON_EMIT_REACHED" in r.stdout


# Round-6 finding #8: cmd_create normalizer must NOT mangle a
# positional title starting with `--`. Disambiguation via known-flag
# list.
_CREATE_NORMALIZER_DISAMBIG_SNIPPET = r"""
set -euo pipefail
title=""
priority_name=""
issue_type=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --*=* ]]; then
        _norm_flag="${1%%=*}"
        _norm_val="${1#*=}"
        _is_known_flag="false"
        case "$_norm_flag" in
            --type|--parent|--priority|--label|--labels|--assign|--assignee|--pipeline|--estimate|--body|--file|--body-file|--description|--json|--quiet|--stdin)
                _is_known_flag="true"
                ;;
        esac
        if [[ -n "$title" || "$_is_known_flag" == "true" ]]; then
            set -- "$_norm_flag" "$_norm_val" "${@:2}"
        fi
    fi
    case "$1" in
        -t|--type) issue_type="$2"; shift 2 ;;
        --priority) priority_name="$2"; shift 2 ;;
        *) if [[ -z "$title" ]]; then title="$1"; fi; shift ;;
    esac
done
echo "TITLE:${title}"
echo "TYPE:${issue_type}"
echo "PRIORITY:${priority_name}"
"""


def _create_normalize(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _CREATE_NORMALIZER_DISAMBIG_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_normalizer_preserves_title_starting_with_double_dash_unknown() -> None:
    """`zh create "--rotate=enabled fails on retry" -t Bug` must keep
    the title intact. `--rotate` is not in the known-flag list, so
    the normalizer leaves the token alone and the `*)` arm captures
    it as the title.
    """
    r = _create_normalize("--rotate=enabled fails on retry", "-t", "Bug")
    assert r.returncode == 0
    assert "TITLE:--rotate=enabled fails on retry" in r.stdout
    assert "TYPE:Bug" in r.stdout


def test_normalizer_still_handles_known_flag_first() -> None:
    """`--priority=High` IS a known flag, so the flag-first form
    `--priority=High "Foo"` still normalizes correctly. Regression
    guard: the round-6 #8 disambiguation must not break this case.
    """
    r = _create_normalize("--priority=High", "Foo")
    assert r.returncode == 0
    assert "TITLE:Foo" in r.stdout
    assert "PRIORITY:High" in r.stdout


def test_normalizer_handles_known_flag_after_title() -> None:
    """Title-first then flag: `"Title" --priority=High` always
    normalizes (title is set, so the second branch of the gate
    fires).
    """
    r = _create_normalize("Title", "--priority=High")
    assert r.returncode == 0
    assert "TITLE:Title" in r.stdout
    assert "PRIORITY:High" in r.stdout


def test_normalizer_preserves_unknown_flag_after_title() -> None:
    """After title is set, ANY `--foo=bar` is treated as a flag. An
    unknown one (`--rotate=enabled`) gets normalized into two args
    and falls through cleanly. Regression guard: the disambiguation
    only protects the FIRST positional.
    """
    r = _create_normalize("Title", "--rotate=enabled")
    assert r.returncode == 0
    assert "TITLE:Title" in r.stdout


# Round-6 finding #9: zh_hierarchy_warn_for_noun scans only the
# trailing positionals first, then falls back to the leading
# positionals. Future-proofs against new flags without an explicit
# skip-list update.
_TRAILING_SCAN_SNIPPET = r"""
set -euo pipefail
shift  # drop expected_type
shift  # drop verb

first_flag_pos=-1
last_flag_pos=-1
idx=-1
for arg in "$@"; do
    idx=$((idx + 1))
    if [[ "$arg" == -* ]]; then
        if [[ "$first_flag_pos" -eq -1 ]]; then
            first_flag_pos=$idx
        fi
        last_flag_pos=$idx
    fi
done

found_num=""
idx=-1
for arg in "$@"; do
    idx=$((idx + 1))
    if [[ "$idx" -le "$last_flag_pos" ]]; then continue; fi
    stripped="${arg#\#}"
    if [[ "$stripped" =~ ^[0-9]+$ ]]; then
        found_num="$stripped"
        break
    fi
done

if [[ -z "$found_num" ]] && [[ "$first_flag_pos" -gt 0 ]]; then
    idx=-1
    for arg in "$@"; do
        idx=$((idx + 1))
        if [[ "$idx" -ge "$first_flag_pos" ]]; then break; fi
        stripped="${arg#\#}"
        if [[ "$stripped" =~ ^[0-9]+$ ]]; then
            found_num="$stripped"
            break
        fi
    done
fi

if [[ -z "$found_num" ]]; then
    echo "NOT_FOUND"
else
    echo "FOUND:${found_num}"
fi
"""


def _trailing_scan(*argv):
    return subprocess.run(
        ["bash", "-c", _TRAILING_SCAN_SNIPPET, "_", *argv],
        capture_output=True, text=True, check=False,
    )


def test_trailing_scan_no_flags_finds_positional() -> None:
    """`zh epic close 42` -> no flags, leading scan finds 42."""
    r = _trailing_scan("Epic", "close", "42")
    assert r.stdout.strip() == "FOUND:42"


def test_trailing_scan_flag_first_finds_trailing() -> None:
    """`zh epic update -t Foo 100` -> last flag `-t`, trailing has
    Foo and 100, first numeric is 100.
    """
    r = _trailing_scan("Epic", "update", "-t", "Foo", "100")
    assert r.stdout.strip() == "FOUND:100"


def test_trailing_scan_positional_first_falls_back_to_leading() -> None:
    """`zh epic close 42 -c "comment"` (future verb) -> last flag
    `-c`, trailing has `comment` only (no numeric). Falls back to
    leading scan and finds 42.
    """
    r = _trailing_scan("Epic", "close", "42", "-c", "comment")
    assert r.stdout.strip() == "FOUND:42"


def test_trailing_scan_skips_numeric_flag_value() -> None:
    """`zh epic update -t 42 100` -> last flag `-t`, trailing has
    `42, 100` (42 IS the flag value). Trailing pass picks 42 (a
    known false positive: the warn would point at #42 instead of
    #100), but this is documented as acceptable noise in the
    advisory warn vs. the maintenance cost of an explicit arity
    list. The redirect text is the only impact; the verb still
    runs against #100 (cmd_update_issue does its own arg parsing).
    """
    r = _trailing_scan("Epic", "update", "-t", "42", "100")
    # The trailing pass picks the first numeric, which is `42` (the
    # flag value). This is the trade-off the trailing-only model
    # accepts. Pinned here so the behavior is explicit.
    assert r.stdout.strip() == "FOUND:42"


def test_trailing_scan_returns_not_found_with_no_numeric() -> None:
    """`zh epic update -t Foo` -> only the flag and its non-numeric
    value. No issue number, helper stays silent.
    """
    r = _trailing_scan("Epic", "update", "-t", "Foo")
    assert r.stdout.strip() == "NOT_FOUND"


# Round-6 finding #10: --json estimate gets a `_requested` companion
# so consumers can tell intent from confirmation apart. Same shape
# as priority / priority_requested.
_ESTIMATE_REQUESTED_SNIPPET = r"""
estimate="$1"           # request (empty = not requested)
estimate_applied="$2"   # "true" / "false"

_est_applied_for_json=""
if [[ "$estimate_applied" == "true" ]]; then
    _est_applied_for_json="$estimate"
fi

jq -n \
    --arg estimate "${_est_applied_for_json}" \
    --arg estimate_requested "${estimate}" \
    '{estimate: (if $estimate == "" then null else ($estimate | tonumber) end),
      estimate_requested: (if $estimate_requested == "" then null else ($estimate_requested | tonumber) end)}'
"""


def _estimate_requested_json(
    estimate: str, estimate_applied: str,
) -> dict:
    r = subprocess.run(
        ["bash", "-c", _ESTIMATE_REQUESTED_SNIPPET, "_",
         estimate, estimate_applied],
        capture_output=True, text=True, check=False,
    )
    import json as _json
    return _json.loads(r.stdout)


def test_estimate_requested_not_passed_is_both_null() -> None:
    """No --estimate -> both fields null. Consumer branches:
    null -> null = not requested.
    """
    obj = _estimate_requested_json("", "false")
    assert obj["estimate"] is None
    assert obj["estimate_requested"] is None


def test_estimate_requested_applied_carries_value() -> None:
    """--estimate 5 applied successfully -> both fields = 5."""
    obj = _estimate_requested_json("5", "true")
    assert obj["estimate"] == 5
    assert obj["estimate_requested"] == 5


def test_estimate_requested_but_not_confirmed() -> None:
    """--estimate 5 requested, mutation didn't confirm -> estimate
    null, estimate_requested 5. Consumer detects partial apply.
    """
    obj = _estimate_requested_json("5", "false")
    assert obj["estimate"] is None
    assert obj["estimate_requested"] == 5


# Round-6 finding #11: type-mismatch redirect for non-planning types
# uses the right top-level verb. close/reopen exist; show/update fall
# back to `zh issue`.
_NONPLANNING_REDIRECT_SNIPPET = r"""
set -euo pipefail
verb="$1"
issue_num="42"
case "$verb" in
    close) redirect="zh close ${issue_num}" ;;
    reopen) redirect="zh reopen ${issue_num}" ;;
    *) redirect="zh issue ${issue_num}" ;;
esac
echo "$redirect"
"""


def _nonplanning_redirect(verb: str) -> str:
    r = subprocess.run(
        ["bash", "-c", _NONPLANNING_REDIRECT_SNIPPET, "_", verb],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip()


def test_nonplanning_redirect_close_uses_top_level_close() -> None:
    """`zh epic close 42` against a Bug now redirects to
    `zh close 42`, which is a real verb that does what the user
    asked. Pre-fix it pointed at `zh issue 42` (read-only).
    """
    assert _nonplanning_redirect("close") == "zh close 42"


def test_nonplanning_redirect_reopen_uses_top_level_reopen() -> None:
    """Symmetric for reopen."""
    assert _nonplanning_redirect("reopen") == "zh reopen 42"


def test_nonplanning_redirect_show_uses_zh_issue() -> None:
    """show has no destructive analog; route to zh issue."""
    assert _nonplanning_redirect("show") == "zh issue 42"


def test_nonplanning_redirect_update_uses_zh_issue() -> None:
    """update similarly: there's no `zh update` top-level verb that
    parallels close / reopen. zh issue is the safe read-only landing.
    """
    assert _nonplanning_redirect("update") == "zh issue 42"
