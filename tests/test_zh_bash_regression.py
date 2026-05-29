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
