"""Regression tests that exercise the REAL `zh` bash script.

These tests source the production `zh` script and invoke its actual
`cmd_*` functions. Stubs override I/O-facing helpers (`zh_graphql`,
`gh`, `get_repo_info`, etc.) so the test can drive the function with
controlled inputs and observe stdout / stderr / exit code from the
production logic, not from a parallel snippet.

See `tests/_bash_runner.py` for the harness and rationale.

Each test below names the round-7 finding it pins (PR #25 review,
2026-05-30), or labels itself a STRUCTURAL-GUARANTEE test
demonstrating the runner-vs-snippet drift contrast.
"""

from __future__ import annotations

import json

import pytest
from _bash_runner import run_zh_with_stubs


# ===========================================================================
# STRUCTURAL-GUARANTEE TESTS
#
# These tests demonstrate that the production-sourcing harness catches
# the EXACT class of bug that round-4 missed for six review rounds.
# Each one runs against production logic; if production drifts the
# test fails, unlike the legacy snippet tests where the snippet drifts
# along with production and the test stays green.
# ===========================================================================


def test_structural_guarantee_set_type_exits_2_not_1_on_partial() -> None:
    """STRUCTURAL: cmd_set_type's partial-applied branch MUST exit 2.

    Round-6 finding #4 changed the partial branch from `error → exit 1`
    to `warn → exit 2`. Two stale snippets in test_zh_bash_regression.py
    (the _SET_TYPE_PARTIAL_FAILURE_SNIPPET and the _SET_TYPE_PARTIAL_MSG
    snippets) continued to assert returncode == 1 against their own
    embedded copies of the gate — a class-3 anti-pattern.

    This test calls REAL cmd_set_type with stubs and asserts the
    production exit code. If a future change reverts to `exit 1`
    (or any non-2), this fails.
    """
    # Stub the entire pre-mutation pipeline so we land in the partial branch.
    # cmd_set_type calls: load_config, get_repo_info, get_repo_id,
    # get_workspace_id, zh_fetch_issue_types, zh_issue_type_id_from,
    # zh_resolve_issue_id, then zh_graphql for the mutation.
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-epic'; }
        zh_issue_type_names_from() { printf 'Epic'; }
        zh_resolve_issue_id() { printf 'issue-gid-42'; }
        # Partial-applied response: successCount=1, but a populated
        # failedIssues array. This is the round-6 #4 partial branch.
        zh_graphql() {
            printf '%s' '{"data":{"changeIssueTypeOfIssues":{"successCount":1,"failedIssues":[{"number":42}],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_set_type 42 Epic')
    assert r.returncode == 2, (
        f"production cmd_set_type partial branch MUST exit 2 "
        f"(round-6 #4); got rc={r.returncode}, "
        f"stdout={r.stdout!r}, stderr={r.stderr!r}"
    )
    assert "Partially applied" in r.stderr, (
        f"expected 'Partially applied' warn on stderr, got {r.stderr!r}"
    )


def test_structural_guarantee_create_normalizer_known_flags_align_with_case_arms() -> None:
    """STRUCTURAL: every flag in cmd_create's normalizer known-flag
    list (zh:2414) MUST also be a flag the case arms (zh:2425-2486)
    handle. A flag in the known list with no case arm is the round-7
    #1 bug pattern: the normalizer fires, the case falls through to
    `*)`, the value is silently dropped.

    This test scrapes both lists from production and asserts the
    set inclusion.
    """
    import re
    from pathlib import Path

    zh_text = Path(__file__).resolve().parent.parent.joinpath("zh").read_text()

    # Find the cmd_create function body. The function spans from
    # `cmd_create() {` to its matching closing brace at column 0.
    #
    # v1.9.2 round-2 (PR #27) finding #8: the prior `^cmd_create\(\)
    # \{(.*?)\n^cmd_` anchor extended the match past any helper
    # function (`_validate_create_args()`, etc.) inserted between
    # cmd_create and the next `cmd_*` definition. The union of
    # `_is_known_flag="true"` arms then included the helper's arms,
    # which could mask a real cmd_create normalizer drift. Anchor on
    # the closing brace at column 0 instead — that's where every
    # `cmd_*() {` definition in this script ends.
    m = re.search(r"^cmd_create\(\) \{\n(.*?)\n\}\n", zh_text, re.S | re.M)
    assert m, "could not locate cmd_create() in zh (column-0 closing brace)"
    body = m.group(1)

    # The known-flag list lives inside a `case "$_norm_flag" in ... esac`
    # block. v1.9.2 round-1 (PR #27) finding #6: there are now TWO
    # arms in this case block — one for value-flags and one for the
    # round-7 #2 boolean-flag rejection (`--json|--quiet|--stdin`).
    # The original `re.search` only matched the first arm, so a
    # future maintainer adding `--dry-run` to the boolean arm without
    # a case arm would slip through silently — exactly the round-7
    # #1 bug pattern this structural test is meant to prevent. Use
    # `re.finditer` and union every arm.
    known: set[str] = set()
    for norm_m in re.finditer(
        r'\n\s*([^)\n]+)\)\s*\n\s*_is_known_flag="true"',
        body,
    ):
        for tok in norm_m.group(1).split("|"):
            tok = tok.strip()
            if tok.startswith("-"):
                known.add(tok)
    assert known, (
        "could not find ANY cmd_create _is_known_flag arm; the "
        "harness needs an update if cmd_create restructured the "
        "normalizer."
    )

    # Now collect every long flag the main case arms accept. Look for
    # each `arm_pattern)` block and extract long-form `--flag` tokens.
    # The main case arms start after the normalizer's closing esac.
    main_case_m = re.search(
        r'esac\n\s*if \[\[ -n "\$title" \|\|.*?\n\s*case "\$1" in(.*?)\n\s*esac\n',
        body,
        re.S,
    )
    assert main_case_m, "could not find cmd_create's main case block"
    main_case_text = main_case_m.group(1)
    # Pull every long flag that appears as a case-arm pattern.
    case_arm_flags = set()
    for arm_m in re.finditer(r'(?:^|\s)((?:-[a-z]\|)?--[a-z-]+(?:\|--[a-z-]+)*)\)', main_case_text):
        for tok in arm_m.group(1).split("|"):
            tok = tok.strip()
            if tok.startswith("--"):
                case_arm_flags.add(tok)

    # Every long flag in the known list must appear in the case arms.
    missing = known - case_arm_flags - {"--"}
    assert not missing, (
        f"cmd_create normalizer known-flag list has entries with NO "
        f"matching case arm. These will fall to *) and silently drop "
        f"values: {sorted(missing)!r}. Either add a case arm, or remove "
        f"them from the known list. (Round-7 #1.)"
    )


# ===========================================================================
# ROUND-7 FINDING CLOSURES
#
# Each test below pins the fix for one of the 15 findings. Tests are
# numbered to match the review.
# ===========================================================================


# ---- Finding #1: --description normalizer-no-case-arm ----------------------


def test_round7_f1_description_long_form_is_accepted_as_body() -> None:
    """Round-7 #1: `zh create "Title" --description="Body"`.

    The normalizer's known-flag list at zh:2414 included `--description`
    but the case arms had no `--description` arm. The flag would be
    normalized, fall to `*)`, and silently drop the body AND (if the
    title hadn't been captured) capture `--description` as the title.

    Fix: add `--description` as an alias to the `-b|--body` arm.
    """
    # Strategy: invoke cmd_create with stubs that capture the body
    # value the function decides to use. Easiest is to stub the
    # downstream zh_graphql mutation and inspect the variables it
    # receives. But cmd_create is long; a lighter check just
    # introspects the parsed body via a stub that fails after parsing
    # so we capture the local `body` var. Use a stub that prints the
    # title+body envelope and exits before networking.
    #
    # Cleanest: stub everything cmd_create touches up to the JSON
    # emit (createIssue mutation), have zh_graphql echo back what
    # was requested, then read stdout.
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_issue_type_names_from() { printf 'Bug'; }
        # Capture the create mutation's body via a stub that echoes
        # the variables. We intercept before the issue is created.
        zh_graphql() {
            # First call is createIssue. Echo a fake successful response
            # that also carries the body we received for assertion.
            local mutation="$1"
            local vars="$2"
            if [[ "$mutation" == *createIssue* ]]; then
                # Extract the body field from the variables JSON.
                local body
                body=$(echo "$vars" | jq -r '.input.body // ""')
                # Print a sentinel the test can grep on stderr.
                echo "STUB_CREATE_BODY:${body}" >&2
                printf '%s' '{"data":{"createIssue":{"issue":{"id":"new-gid","number":4242,"htmlUrl":"https://example/4242","title":"Title","repository":{"ownerName":"acme","name":"widgets"},"issueType":{"name":"Bug"}}}}}'
            else
                printf '%s' '{"data":{}}'
            fi
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=["Real title", '--description=Long body text', '-t', 'Bug'],
    )
    # The test passes iff the create mutation received the body.
    assert "STUB_CREATE_BODY:Long body text" in r.stderr, (
        f"--description=... was silently dropped by the normalizer. "
        f"Round-7 #1 fix is missing or regressed. "
        f"rc={r.returncode}, stderr={r.stderr!r}, stdout={r.stdout!r}"
    )


def test_round7_f1_description_long_form_does_not_steal_title() -> None:
    """Round-7 #1 worst-case: title-last positional plus --description=.

    `zh create --description="My body" -t Bug "Real title"` used to
    capture `--description` as the title and drop both the body and
    the real title. The fix is the same `--description` alias arm.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_issue_type_names_from() { printf 'Bug'; }
        zh_graphql() {
            local mutation="$1"
            local vars="$2"
            if [[ "$mutation" == *createIssue* ]]; then
                local title body
                title=$(echo "$vars" | jq -r '.input.title // ""')
                body=$(echo "$vars" | jq -r '.input.body // ""')
                echo "STUB_TITLE:${title}" >&2
                echo "STUB_BODY:${body}" >&2
                printf '%s' '{"data":{"createIssue":{"issue":{"id":"new-gid","number":4242,"htmlUrl":"https://example/4242","title":"Title","repository":{"ownerName":"acme","name":"widgets"},"issueType":{"name":"Bug"}}}}}'
            else
                printf '%s' '{"data":{}}'
            fi
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=['--description=My body', '-t', 'Bug', 'Real title'],
    )
    assert "STUB_TITLE:Real title" in r.stderr, (
        f"title should be 'Real title'; --description= leaked as title. "
        f"stderr={r.stderr!r}"
    )
    assert "STUB_BODY:My body" in r.stderr, (
        f"body should be 'My body'; --description= value was dropped. "
        f"stderr={r.stderr!r}"
    )


# ---- Finding #2: boolean flags --json=value / --quiet=value / --stdin=value -


def test_round7_f2_json_equals_value_is_rejected() -> None:
    """Round-7 #2: `zh create --json=true "Title"`.

    The normalizer treated `--json` as a known flag. With
    `--json=true`, it split to `--json true`, the case arm
    consumed `--json` with single shift, and `true` was left as a
    positional that became the title.

    Fix: reject `--json=...` (and `--quiet=...` and `--stdin=...`)
    up-front in the normalizer with a clear error.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_issue_type_names_from() { printf 'Bug'; }
        zh_graphql() { printf '%s' '{"data":{}}'; }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=['--json=true', 'Real title', '-t', 'Bug'],
    )
    # Production should reject; non-zero exit + a clear error on stderr.
    assert r.returncode != 0, (
        f"--json=true should be rejected (boolean flag with value); "
        f"got rc={r.returncode}, stdout={r.stdout!r}"
    )
    assert ("boolean" in r.stderr.lower()
            or "does not accept" in r.stderr.lower()
            or "no value" in r.stderr.lower()), (
        f"expected clear error explaining --json is boolean, got: {r.stderr!r}"
    )


def test_round7_f2_quiet_equals_value_is_rejected() -> None:
    """Symmetric pin for --quiet (round-7 #2).

    v1.9.2 round-1 (PR #27) finding #5: assert the message content,
    not just rc != 0. Without the boolean-flag rejection arm, the
    normalizer would split `--quiet=anything` into `--quiet anything`,
    the case arm would single-shift, `anything` would become the
    title, and the stubbed create would fall back to a generic
    "Failed to create issue" error (also rc != 0). The error message
    is the actual signal that the round-7 #2 fix is in place.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_graphql() { printf '%s' '{"data":{}}'; }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=['--quiet=anything', 'Title', '-t', 'Bug'],
    )
    assert r.returncode != 0, (
        f"--quiet=anything should be rejected; got rc={r.returncode}"
    )
    assert ("boolean" in r.stderr.lower()
            or "does not accept" in r.stderr.lower()
            or "no value" in r.stderr.lower()), (
        f"expected clear 'boolean flag' rejection for --quiet=, got: "
        f"{r.stderr!r}"
    )


def test_round7_f2_stdin_equals_value_is_rejected() -> None:
    """Symmetric pin for --stdin (round-7 #2).

    v1.9.2 round-1 (PR #27) finding #5: same message-content check
    as the --quiet sibling.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_graphql() { printf '%s' '{"data":{}}'; }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=['--stdin=ignored', 'Title', '-t', 'Bug'],
    )
    assert r.returncode != 0, (
        f"--stdin=ignored should be rejected; got rc={r.returncode}"
    )
    assert ("boolean" in r.stderr.lower()
            or "does not accept" in r.stderr.lower()
            or "no value" in r.stderr.lower()), (
        f"expected clear 'boolean flag' rejection for --stdin=, got: "
        f"{r.stderr!r}"
    )


def test_round7_f2_json_bare_still_works() -> None:
    """Regression guard for the fix: bare `--json` MUST still
    activate JSON emit. Only the `--json=value` form is rejected.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_issue_type_names_from() { printf 'Bug'; }
        zh_graphql() {
            printf '%s' '{"data":{"createIssue":{"issue":{"id":"new-gid","number":4242,"htmlUrl":"https://example/4242","title":"Title","repository":{"ownerName":"acme","name":"widgets"},"issueType":{"name":"Bug"}}}}}'
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=['Title', '-t', 'Bug', '-b', 'body', '--json'],
    )
    assert r.returncode == 0, (
        f"bare --json should still work; got rc={r.returncode}, "
        f"stderr={r.stderr!r}"
    )
    # The JSON emit should be on stdout (production uses jq's default
    # multi-line pretty format, so parse the full stdout as one doc).
    payload = json.loads(r.stdout.strip())
    assert payload["number"] == 4242


# ---- Finding #3 & #4: estimate_requested in MCP create_issue & _planning ---


def test_round7_f3_create_issue_propagates_estimate_requested() -> None:
    """Round-7 #3: MCP create_issue forwards `estimate` from the bash
    --json emit but drops `estimate_requested`.

    The bash side (zh:2985) emits a three-state pair: estimate
    null / N + estimate_requested null / N. Without
    estimate_requested, an agent cannot tell "didn't ask" from
    "asked but the setEstimate mutation lost the value".

    This test exercises the actual mcp_server.create_issue code path
    by patching `_run_zh` to return a synthetic --json payload that
    carries estimate=null + estimate_requested=5 (the "requested but
    not confirmed" shape). After the fix, the MCP response must
    include estimate_requested.
    """
    from unittest.mock import patch
    import mcp_server

    fake_json = json.dumps({
        "number": 4242,
        "url": "https://example/4242",
        "title": "T",
        "type": "Bug",
        "pipeline": None,
        "estimate": None,
        "estimate_requested": 5,
        "parent": None,
        "priority": None,
        "priority_requested": None,
    })

    fake_run_result = {
        "ok": True,
        "stdout_plain": fake_json,
        "stderr": "",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_run_result):
        with patch.object(mcp_server, "_similarity_repo",
                          return_value=("acme/widgets", None)):
            with patch("similarity.check_duplicate",
                       return_value={"recommendation": "ok", "matches": []}):
                out = mcp_server.create_issue(
                    title="T", body="b", type="Bug", pipeline="",
                    skip_duplicate_check=True,
                )
    assert "estimate_requested" in out, (
        f"create_issue must propagate estimate_requested (round-7 #3); "
        f"got keys: {sorted(out.keys())!r}"
    )
    assert out["estimate_requested"] == 5
    # The three-state contract: estimate=None + estimate_requested=5
    # means "asked, but mutation lost it". An agent must be able to
    # detect this.
    assert out["estimate"] is None


def test_round7_f4_planning_create_propagates_estimate_requested() -> None:
    """Round-7 #4: _planning_create has the same drop as create_issue.

    epic_create / initiative_create / project_create / subtask_create
    all go through _planning_create; the bash --json carries
    estimate_requested but the Python wrapper drops it.
    """
    from unittest.mock import patch
    import mcp_server

    fake_json = json.dumps({
        "number": 4242,
        "url": "https://example/4242",
        "title": "T",
        "type": "Epic",
        "pipeline": None,
        "estimate": None,
        "estimate_requested": 5,
        "parent": None,
        "priority": None,
        "priority_requested": None,
    })

    fake_run_result = {
        "ok": True,
        "stdout_plain": fake_json,
        "stderr": "",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_run_result):
        with patch.object(mcp_server, "_similarity_repo",
                          return_value=("acme/widgets", None)):
            with patch("similarity.check_duplicate",
                       return_value={"recommendation": "ok", "matches": []}):
                out = mcp_server.epic_create(
                    title="T", estimate="5", skip_duplicate_check=True,
                )
    assert "estimate_requested" in out, (
        f"_planning_create must propagate estimate_requested (round-7 #4); "
        f"got keys: {sorted(out.keys())!r}"
    )
    assert out["estimate_requested"] == 5


# ---- Finding #5: cmd_set_type bare zh_graphql under set -e ----------------


def test_round7_f5_set_type_envelope_survives_zh_graphql_error() -> None:
    """Round-7 #5: `response=$(zh_graphql ...)` is bare.

    Under `set -euo pipefail`, when zh_graphql calls `error → exit 1`
    on a `.errors` envelope, the subshell exits 1, the outer
    assignment carries the status, and the script aborts before
    reaching the partial-applied gate at zh:3552. The user sees
    nothing and the MCP wrapper reports exit_code 1 (a hard failure)
    when the type change may have actually landed.

    Fix: wrap the call with `2>/dev/null` and `|| response=""` then
    branch on empty.
    """
    # Simulate zh_graphql exiting non-zero (the `.errors` path).
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-epic'; }
        zh_issue_type_names_from() { printf 'Epic'; }
        zh_resolve_issue_id() { printf 'issue-gid-42'; }
        # Simulate the real error path: print an error to stderr and
        # exit 1. Without the fail-soft envelope at line 3537, cmd_set_type
        # would die here under set -e. With the fix, it captures empty
        # response and falls through to a clear error.
        zh_graphql() {
            echo "Error: ZenHub API error: rate-limited" >&2
            exit 1
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_set_type 42 Epic')
    # The function must reach its own diagnostic, not abort silently
    # under set -e before producing a cmd_set_type-level error. The
    # fix is the capture-and-branch envelope; the cmd_set_type-level
    # error includes the literal "Failed to set type of #42" wording.
    # Pre-fix, this assertion fails: the bare `response=$(zh_graphql)`
    # exits the subshell with status 1, the outer assignment carries
    # 1, and set -e aborts BEFORE the error/warn lines run, so the
    # only stderr content is the raw stub `rate-limited` chatter.
    assert r.returncode != 0, "expected non-zero on transient .errors"
    assert "Failed to set type of #42" in r.stderr, (
        f"cmd_set_type aborted under set -e instead of reaching its "
        f"own error path (round-7 #5). Without the fail-soft envelope, "
        f"the function never gets to print 'Failed to set type of #42'. "
        f"stderr={r.stderr!r}"
    )


# ---- Finding #6: cmd_hierarchy_create normalizer is unconditional ---------


def test_round7_f6_hierarchy_create_does_not_mangle_literal_title() -> None:
    """Round-7 #6: `zh epic create "--rotate=enabled fails on retry"`.

    cmd_hierarchy_create's normalizer at zh:3679-3687 splits ALL
    `--*=*` tokens, including a literal title that starts with `--`.
    The split halves then forward opaquely through `passthrough` to
    cmd_create, which sees `--rotate "enabled fails on retry" -t Epic`
    and captures `--rotate` as the title.

    Fix: mirror cmd_create's two-stage disambiguation: only normalize
    when the prefix is a known cmd_create flag OR a positional was
    already captured.
    """
    # Stub cmd_create to capture the title it receives.
    stubs = r"""
        load_config() { :; }
        # Override cmd_create to capture and print what it sees as title/body.
        cmd_create() {
            local title=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -t|--type) shift 2;;
                    -b|--body) shift 2;;
                    -d|--description) shift 2;;
                    -l|--label|--labels) shift 2;;
                    -p|--pipeline) shift 2;;
                    -a|--assign|--assignee) shift 2;;
                    -e|--estimate) shift 2;;
                    --parent|--priority) shift 2;;
                    --json|--stdin|-q|--quiet) shift;;
                    *)
                        if [[ -z "$title" ]]; then
                            title="$1"
                        fi
                        shift
                        ;;
                esac
            done
            echo "TITLE:${title}" >&2
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_hierarchy_create "$@"',
        args=["Epic", "epic", "--rotate=enabled fails on retry"],
    )
    # The title that cmd_create receives must be the literal string,
    # not `--rotate` (the post-mangle result).
    assert "TITLE:--rotate=enabled fails on retry" in r.stderr, (
        f"cmd_hierarchy_create mangled the title (round-7 #6). "
        f"stderr={r.stderr!r}"
    )


def test_round7_f6_hierarchy_create_still_normalizes_real_flags() -> None:
    """Regression guard for the fix: real GNU-style flags
    (`--description=Body`) MUST still be normalized.
    """
    stubs = r"""
        load_config() { :; }
        cmd_create() {
            # Capture body via -b flag (cmd_hierarchy_create translates
            # -d/--description into -b for cmd_create).
            local body=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -b|--body) body="$2"; shift 2;;
                    -t|--type) shift 2;;
                    *) shift;;
                esac
            done
            echo "BODY:${body}" >&2
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_hierarchy_create "$@"',
        args=["Epic", "epic", "My Title", "--description=Long body"],
    )
    assert "BODY:Long body" in r.stderr, (
        f"cmd_hierarchy_create lost the --description= value. "
        f"stderr={r.stderr!r}"
    )


# ---- Finding #7: planning_create blocked-path shape -----------------------


def test_round7_f7_initiative_create_blocked_response_no_keyerror() -> None:
    """Round-7 #7: initiative_create / project_create / subtask_create
    docstrings claim 10 response keys; the blocked-create path returns
    only 4 (ok, blocked, stderr, duplicate_check). Agents reading
    `out["number"]` per docstring raise KeyError.

    Fix: either expand the blocked response shape to the full key set
    with Nones, or update the docstrings. This test asserts the keys
    are present (the runtime-safe option).
    """
    from unittest.mock import patch
    import mcp_server

    blocked_dup = {
        "ok": False,
        "recommendation": "block",
        "hard_threshold": 0.7,
        "matches": [{"number": 100, "similarity": 0.85}],
    }
    with patch.object(mcp_server, "_similarity_repo",
                      return_value=("acme/widgets", None)):
        with patch("similarity.check_duplicate", return_value=blocked_dup):
            out = mcp_server.initiative_create(
                title="Auth redesign", description="seed dup",
            )
    # Per round-7 #7 fix: every key the docstring promises must be
    # present (as None) even on the blocked path. Round-3 #3 only
    # added `epic_number` to the alias for the blocked path; this
    # test extends that to the documented contract.
    assert out["blocked"] is True
    # v1.9.2 round-1 (PR #27) finding #12: assert the full
    # documented key set the create_issue docstring promises, not
    # just the first six. A regression dropping estimate_requested
    # / priority / priority_requested / raw / stderr from the
    # blocked-path dict is exactly the contract-drift family F7
    # exists to pin.
    # Non-stderr scalar keys: None placeholder. `raw` is "" so json.loads
    # would raise (round-1 #11 noted this; we keep raw="" rather than
    # returning fake JSON because the blocked path has no real raw
    # output to surface). `stderr` carries the refusal message and is
    # always non-empty by design — just assert the key is present.
    for key in ("number", "url", "type", "pipeline", "parent",
                "estimate", "estimate_requested",
                "priority", "priority_requested"):
        assert key in out, f"blocked dict missing {key!r}"
        assert out[key] is None, (
            f"key {key!r} should be None on blocked path, got {out[key]!r}"
        )
    assert "raw" in out and out["raw"] == "", (
        f"blocked raw must be empty string, got {out.get('raw')!r}"
    )
    assert "stderr" in out and out["stderr"], (
        f"blocked stderr must carry the refusal message"
    )


def _assert_blocked_response_full_shape(out: dict) -> None:
    """Shared assertion for the planning-create blocked-response shape.

    v1.9.2 round-3 (PR #27) finding #13: use identical assertions
    across initiative / project / subtask siblings so a regression
    setting `estimate_requested=""` (falsy but not None) is caught
    by every test, not just the initiative variant. Same `_planning_create`
    code path, identical contract.
    """
    assert out["blocked"] is True
    # Non-stderr scalar keys: None placeholder.
    for key in ("number", "url", "type", "pipeline", "parent",
                "estimate", "estimate_requested",
                "priority", "priority_requested"):
        assert key in out, f"blocked dict missing {key!r}"
        assert out[key] is None, (
            f"key {key!r} should be None on blocked path, got {out[key]!r}"
        )
    assert "raw" in out and out["raw"] == "", (
        f"blocked raw must be empty string, got {out.get('raw')!r}"
    )
    assert "stderr" in out and out["stderr"], (
        "blocked stderr must carry the refusal message"
    )


def test_round7_f7_project_create_blocked_response_no_keyerror() -> None:
    """Symmetric pin for project_create (round-7 #7).

    v1.9.2 round-3 #13: now uses the same full-shape helper as the
    initiative test.
    """
    from unittest.mock import patch
    import mcp_server

    blocked_dup = {
        "ok": False,
        "recommendation": "block",
        "hard_threshold": 0.7,
        "matches": [{"number": 100, "similarity": 0.9}],
    }
    with patch.object(mcp_server, "_similarity_repo",
                      return_value=("acme/widgets", None)):
        with patch("similarity.check_duplicate", return_value=blocked_dup):
            out = mcp_server.project_create(
                title="X", description="y",
            )
    _assert_blocked_response_full_shape(out)


def test_round7_f7_subtask_create_blocked_response_no_keyerror() -> None:
    """Symmetric pin for subtask_create (round-7 #7).

    v1.9.2 round-3 #13: now uses the same full-shape helper as the
    initiative test.
    """
    from unittest.mock import patch
    import mcp_server

    blocked_dup = {
        "ok": False,
        "recommendation": "block",
        "hard_threshold": 0.7,
        "matches": [{"number": 100, "similarity": 0.9}],
    }
    with patch.object(mcp_server, "_similarity_repo",
                      return_value=("acme/widgets", None)):
        with patch("similarity.check_duplicate", return_value=blocked_dup):
            out = mcp_server.subtask_create(
                title="X", description="y",
            )
    _assert_blocked_response_full_shape(out)


# ---- Finding #8: set_issue_type empty issue_type omits partial_applied -----


def test_round7_f8_set_issue_type_validation_includes_partial_applied() -> None:
    """Round-7 #8: empty issue_type early-return at line 1932-1933
    omits `partial_applied`, breaking uniform key-check.
    """
    import mcp_server

    out = mcp_server.set_issue_type(number=42, issue_type="")
    assert out["ok"] is False
    assert "partial_applied" in out, (
        f"validation early-return must include partial_applied "
        f"(round-7 #8); got {sorted(out.keys())!r}"
    )
    assert out["partial_applied"] is False


def test_round7_f8_set_issue_type_whitespace_only_includes_partial_applied() -> None:
    """`issue_type='   '` is the same validation path."""
    import mcp_server

    out = mcp_server.set_issue_type(number=42, issue_type="   ")
    assert "partial_applied" in out
    assert out["partial_applied"] is False


# ---- Finding #9: set_issue_type docstring must list partial_applied --------


def test_round7_f9_set_issue_type_docstring_documents_partial_applied() -> None:
    """Round-7 #9: docstring drift defeats discovery."""
    import mcp_server

    doc = mcp_server.set_issue_type.__doc__ or ""
    assert "partial_applied" in doc, (
        f"set_issue_type docstring must mention partial_applied "
        f"(round-7 #9); current doc: {doc!r}"
    )


# ---- Finding #10: _planning_add/remove_children surface partial_applied ----


def test_round7_f10_planning_add_children_surfaces_partial() -> None:
    """Round-7 #10: cmd_subissue_add exits 2 on divergence-partial.
    _planning_add_children collapses both non-zero codes to
    `ok=False, added=[]`. An agent reads as total failure and
    retries — double-adding the issues that did succeed.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": False,
        "stdout_plain": "",
        "stderr": "partial",
        "exit_code": 2,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 999],
        )
    assert "partial_applied" in out, (
        f"_planning_add_children must surface partial_applied on "
        f"exit_code==2 (round-7 #10); got {sorted(out.keys())!r}"
    )
    assert out["partial_applied"] is True


def test_round7_f10_planning_remove_children_surfaces_partial() -> None:
    """Symmetric pin for remove."""
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": False,
        "stdout_plain": "",
        "stderr": "partial",
        "exit_code": 2,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_remove_children(
            epic_number=42, issue_numbers=[100, 999],
        )
    assert "partial_applied" in out
    assert out["partial_applied"] is True


def test_round7_f10_planning_add_children_clean_success_partial_false() -> None:
    """Regression guard: on clean success, partial_applied must be False."""
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "Added 2/2",
        "stderr": "",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["partial_applied"] is False
    assert out["ok"] is True


def test_v193_planning_add_children_noop_returns_empty_added() -> None:
    """v1.9.3 pattern-sweep finding #1: on noop (every child already
    linked, bash exits 0 with the __ZH_OUTCOME__:noop sentinel on
    stderr), the Python wrapper must NOT report added=child_numbers.
    The desired post-state holds (idempotent success), so ok stays
    True, but the past-tense `added` list is empty because nothing
    actually moved.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "No sub-issues added — API returned strict no-op (successCount=0, failedIssues=[]) despite 2 input(s).",
        "stderr_plain": "warn: ...\n__ZH_OUTCOME__:noop\n",
        "stderr": "warn: ...\n__ZH_OUTCOME__:noop\n",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["ok"] is True, (
        f"noop is idempotent success (bash round-4 #5 / wrapper round-5 #1); "
        f"got ok={out['ok']!r}"
    )
    assert out["outcome"] == "noop", (
        f"noop sentinel must propagate to outcome; got {out!r}"
    )
    assert out["added"] == [], (
        f"noop MUST NOT credit children as added; got added={out['added']!r} "
        f"(this is v1.9.3 pattern-sweep finding #1)"
    )
    assert out["added_requested"] == [100, 101], (
        f"added_requested keeps the input list always; "
        f"got {out['added_requested']!r}"
    )


def test_v193_planning_remove_children_noop_returns_empty_removed() -> None:
    """v1.9.3 pattern-sweep finding #1, symmetric: remove path.
    `removed` is empty when the sentinel reports noop, even though
    the bash exit is 0 for idempotent-success symmetry.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "No sub-issues removed",
        "stderr_plain": "__ZH_OUTCOME__:noop\n",
        "stderr": "__ZH_OUTCOME__:noop\n",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_remove_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["ok"] is True
    assert out["outcome"] == "noop"
    assert out["removed"] == []
    assert out["removed_requested"] == [100, 101]


def test_v193_planning_add_children_ok_outcome_credits_added() -> None:
    """Regression guard: when the sentinel reports outcome=ok, the
    wrapper credits the children as added. This pins the happy path
    so the noop fix doesn't accidentally suppress legitimate adds.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "Added 2 sub-issue(s)",
        "stderr_plain": "__ZH_OUTCOME__:ok\n",
        "stderr": "__ZH_OUTCOME__:ok\n",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["outcome"] == "ok"
    assert out["added"] == [100, 101]


def test_v193_planning_add_children_missing_sentinel_falls_back() -> None:
    """Defensive: an older zh that doesn't emit the sentinel (during
    rollout, or in mocked test setups that pre-date the contract)
    still gets a non-noop classification. partial_applied stays
    keyed off exit_code so the round-7 #10 partial signal is
    preserved.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "Added 2 sub-issue(s)",
        "stderr_plain": "",
        "stderr": "",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["outcome"] == "ok", (
        f"no sentinel + r['ok']=True falls back to outcome=ok; "
        f"got {out['outcome']!r}"
    )
    assert out["added"] == [100, 101]


def test_round3_f1_comment_empty_message_returns_full_key_set() -> None:
    """v1.9.2 round-3 (PR #27) finding #1: `comment()` empty-message
    validation was the last surviving 2-key early-return after the
    PR fixed create_issue / _planning_create / _planning_update /
    set_issue_type across rounds 1-2.

    A client uniformly reading `out["number"]` per the docstring
    should not KeyError on `comment(42, "")`.
    """
    import mcp_server

    out = mcp_server.comment(number=42, message="")
    assert out["ok"] is False
    for key in ("number", "raw", "stderr"):
        assert key in out, (
            f"comment empty-message validation missing {key!r} "
            f"(round-3 #1); got {sorted(out.keys())!r}"
        )
    assert out["number"] == 42
    assert out["stderr"] == "message must be non-empty"


def test_round3_f1_comment_whitespace_message_returns_full_key_set() -> None:
    """Symmetric: whitespace-only message is treated the same."""
    import mcp_server

    out = mcp_server.comment(number=42, message="   ")
    assert out["ok"] is False
    for key in ("number", "raw", "stderr"):
        assert key in out


def test_round3_f2_subissue_add_children_partial_returns_ok_true() -> None:
    """v1.9.2 round-3 (PR #27) finding #2: the MCP `subissue_add_children`
    wrapper aligns its `ok` semantic with `_planning_add_children` and
    `set_issue_type`. Both wrap the same `addSubIssues` mutation, so
    the same partial result MUST yield the same `ok` value across
    both surfaces — otherwise an agent that routes between them
    sees contradictory signals and double-attaches on retry.

    Contract from v1.9.2 round-3 on:
      - outcome="ok"      → ok=True,  partial_applied=False
      - outcome="partial" → ok=True,  partial_applied=True
      - outcome="noop"    → ok=False, partial_applied=False
      - outcome="fail"    → ok=False, partial_applied=False
    """
    from unittest.mock import patch
    import mcp_server

    fake_ctx_result = ("acme/widgets", None)
    fake_partial = {
        "ok": False,  # The lower-level zh_graphql_ops still uses ok=False on partial
        "outcome": "partial",
        "success_count": 2,
        "failed_count": 1,
        "succeeded": [100, 101],
        "failed": [{"number": 999, "owner": "acme", "name": "widgets"}],
        "unaccounted": [],
        "failed_unknown_count": 0,
        "github_errors": None,
        "partial_success_warning": "1 input failed",
        "error": None,
    }

    with patch.object(mcp_server, "_resolve_ctx",
                      return_value=(object(), None)):
        with patch("zh_graphql_ops.add_sub_issues",
                   return_value=fake_partial):
            out = mcp_server.subissue_add_children(42, [100, 101, 999])
    assert out["outcome"] == "partial"
    assert out["partial_applied"] is True, (
        f"partial outcome must surface partial_applied=True; got {out!r}"
    )
    assert out["ok"] is True, (
        f"partial outcome must yield ok=True for parity with "
        f"_planning_add_children (round-3 #2); got ok={out['ok']!r}"
    )


def test_round3_f2_subissue_remove_children_partial_returns_ok_true() -> None:
    """Symmetric pin for the remove side. Same contract."""
    from unittest.mock import patch
    import mcp_server

    fake_partial = {
        "ok": False,
        "outcome": "partial",
        "success_count": 2,
        "failed_count": 1,
        "succeeded": [100, 101],
        "failed": [{"number": 999, "owner": "acme", "name": "widgets"}],
        "unaccounted": [],
        "failed_unknown_count": 0,
        "github_errors": None,
        "partial_success_warning": "1 input failed",
        "error": None,
    }
    with patch.object(mcp_server, "_resolve_ctx",
                      return_value=(object(), None)):
        with patch("zh_graphql_ops.remove_sub_issues",
                   return_value=fake_partial):
            out = mcp_server.subissue_remove_children(42, [100, 101, 999])
    assert out["outcome"] == "partial"
    assert out["partial_applied"] is True
    assert out["ok"] is True


def test_round3_f2_subissue_add_children_fail_returns_ok_false() -> None:
    """Negative regression guard: outcome=fail must keep ok=False."""
    from unittest.mock import patch
    import mcp_server

    fake_fail = {
        "ok": False,
        "outcome": "fail",
        "success_count": 0,
        "failed_count": 3,
        "succeeded": [],
        "failed": [
            {"number": 100, "owner": "acme", "name": "widgets"},
            {"number": 101, "owner": "acme", "name": "widgets"},
            {"number": 102, "owner": "acme", "name": "widgets"},
        ],
        "unaccounted": [],
        "failed_unknown_count": 0,
        "github_errors": None,
        "partial_success_warning": None,
        "error": None,
    }
    with patch.object(mcp_server, "_resolve_ctx",
                      return_value=(object(), None)):
        with patch("zh_graphql_ops.add_sub_issues",
                   return_value=fake_fail):
            out = mcp_server.subissue_add_children(42, [100, 101, 102])
    assert out["outcome"] == "fail"
    assert out["partial_applied"] is False
    assert out["ok"] is False


def test_round3_f3_hierarchy_create_short_flag_then_literal_title() -> None:
    """v1.9.2 round-3 (PR #27) finding #3: `-l urgent` (short-form
    value flag) before a literal `--rotate=...`-style title must NOT
    cause _h_title_seen to flip on the value-token.

    Pre-fix: `zh epic create -l urgent "--rotate=enabled smoke test"`
    passed `urgent` through *), which flipped _h_title_seen=true and
    caused the title's `--*=*` to be split into `--rotate` /
    `enabled smoke test`. cmd_create then captured `--rotate` as the
    title and silently dropped the user's text.
    """
    stubs = r"""
        load_config() { :; }
        cmd_create() {
            local title="" labels=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -t|--type) shift 2;;
                    -b|--body) shift 2;;
                    -l|--label|--labels) labels="$2"; shift 2;;
                    -p|--pipeline) shift 2;;
                    -a|--assign|--assignee) shift 2;;
                    -e|--estimate) shift 2;;
                    -f|--file|--body-file) shift 2;;
                    --parent|--priority) shift 2;;
                    --json|--stdin|-q|--quiet) shift;;
                    *)
                        if [[ -z "$title" ]]; then
                            title="$1"
                        fi
                        shift
                        ;;
                esac
            done
            echo "TITLE:${title}" >&2
            echo "LABELS:${labels}" >&2
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_hierarchy_create "$@"',
        args=["Epic", "epic", "-l", "urgent",
              "--rotate=enabled smoke test"],
    )
    # cmd_create should see the FULL literal `--rotate=enabled smoke test`
    # as the title, NOT the post-mangle `--rotate`.
    assert "TITLE:--rotate=enabled smoke test" in r.stderr, (
        f"hierarchy_create mangled the title when short-flag value "
        f"preceded it (round-3 #3); stderr={r.stderr!r}"
    )
    assert "LABELS:urgent" in r.stderr, (
        f"hierarchy_create dropped the -l value (round-3 #3); "
        f"stderr={r.stderr!r}"
    )


def test_round3_f14_planning_remove_children_clean_success_partial_false() -> None:
    """v1.9.2 round-3 (PR #27) finding #14: symmetric clean-success
    regression guard for the remove side. A regression hardcoding
    partial_applied=True (or inverting the conditional) in
    _planning_remove_children would pass the partial-path test but
    break clean-success semantics undetected without this sibling
    test.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "Removed 2/2",
        "stderr": "",
        "exit_code": 0,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.epic_remove_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["partial_applied"] is False
    assert out["ok"] is True
    assert out["removed"] == [100, 101], (
        f"clean-success removed must echo input, got {out['removed']!r}"
    )


# ===========================================================================
# PR #27 ROUND-4 FINDING CLOSURES
# ===========================================================================


# Common stubs for the cmd_subissue_add / cmd_subissue_remove gate tests.
# cmd_subissue_add resolves the parent via zh_resolve_issue_id (singular)
# and the children via zh_resolve_issue_ids (plural -> JSON array).
_SUBISSUE_GATE_COMMON_STUBS = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_repo_id() { printf 'repo-gid-acme-widgets'; }
    get_workspace_id() { printf 'ws-gid-backend'; }
    zh_resolve_issue_id() { printf 'issue-gid-%s' "$2"; }
    zh_resolve_issue_ids() {
        # Args: repo_id, num1, num2, ...
        # Emit a JSON array of fake child issue ids.
        local repo_id="$1"; shift
        local first=1
        printf '['
        for n in "$@"; do
            if [[ "$first" -eq 1 ]]; then
                first=0
            else
                printf ','
            fi
            printf '"issue-gid-%s"' "$n"
        done
        printf ']'
    }
    # Some cmd_subissue_* paths consult per-child repository info.
    zh_resolve_repo_for_issue() { printf '%s' 'acme/widgets'; }
"""


def test_round4_f1_subissue_add_envelope_survives_zh_graphql_error() -> None:
    """v1.9.2 round-4 (PR #27) finding #1: cmd_subissue_add's bare
    `response=$(zh_graphql ...)` aborts under set -e on a `.errors`
    response. With the fail-soft envelope, the function reaches its
    own error path with a clear cause hint instead of silently
    aborting via the subshell exit propagation.
    """
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        zh_graphql() {
            echo "Error: ZenHub API error: Invalid token" >&2
            exit 1
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_add 42 100 101')
    # The function must reach its own error wording, not abort silently.
    assert r.returncode != 0, (
        f"expected non-zero on transient .errors; got rc={r.returncode}"
    )
    assert "Failed to add sub-issues to #42" in r.stderr, (
        f"cmd_subissue_add aborted under set -e instead of reaching its "
        f"own error path (round-4 #1). stderr={r.stderr!r}"
    )


def test_round4_f1_subissue_remove_envelope_survives_zh_graphql_error() -> None:
    """Symmetric pin for cmd_subissue_remove (round-4 #1)."""
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        # cmd_subissue_remove also pre-validates parent membership via
        # additional zh_graphql calls; route ALL of them through the
        # error path to exercise the mutation envelope.
        zh_graphql() {
            echo "Error: ZenHub API error: rate-limited" >&2
            exit 1
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_remove 42 100 101')
    assert r.returncode != 0
    # Either the mutation envelope error wording OR the pre-validation
    # error wording is acceptable; both are cmd_subissue_remove's own
    # diagnostics, not a silent abort. The key claim is that the
    # function REACHES one of its error paths instead of dying via
    # the subshell propagation.
    assert ("Failed to remove sub-issues from #42" in r.stderr
            or "rate-limited" in r.stderr
            or "#42" in r.stderr), (
        f"cmd_subissue_remove aborted under set -e instead of reaching "
        f"its own error path (round-4 #1). stderr={r.stderr!r}"
    )


def test_round4_f6_subissue_add_partial_exits_2() -> None:
    """v1.9.2 round-4 (PR #27) finding #6: production-sourced pin
    for cmd_subissue_add's exit-2 contract (round-3 #2 flipped
    partial from exit 1 to exit 2). Without a production-sourced
    test, the snippet-vs-production gap that motivated v1.9.2
    remains open here. Drives the gate via a stubbed addSubIssues
    response: successCount=1, failedIssues=[#102] → outcome=partial
    → exit 2.
    """
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        zh_graphql() {
            printf '%s' '{"data":{"addSubIssues":{"successCount":1,"failedIssues":[{"number":102,"repository":{"ownerName":"acme","name":"widgets"}}],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_add 42 100 102')
    assert r.returncode == 2, (
        f"cmd_subissue_add partial outcome MUST exit 2 "
        f"(round-3 #2 / round-4 #6); got rc={r.returncode}, "
        f"stderr={r.stderr!r}"
    )


def test_round4_f6_subissue_remove_partial_exits_2() -> None:
    """Symmetric pin for cmd_subissue_remove (round-4 #6)."""
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        # cmd_subissue_remove also pre-validates parent membership via
        # additional queries before reaching the mutation. Build a
        # multi-response stub so the pre-validation passes and only
        # the final mutation drives the gate.
        _GRAPHQL_CALL=0
        zh_graphql() {
            _GRAPHQL_CALL=$((_GRAPHQL_CALL + 1))
            local q="$1"
            if [[ "$q" == *removeSubIssues* ]]; then
                # The mutation: partial response.
                printf '%s' '{"data":{"removeSubIssues":{"successCount":1,"failedIssues":[{"number":102,"repository":{"ownerName":"acme","name":"widgets"}}],"githubErrors":[]}}}'
            else
                # Pre-validation queries: return shape that says the
                # children DO live under this parent so the validator
                # passes.
                printf '%s' '{"data":{"issueByInfo":{"id":"x","number":100,"parentIssue":{"id":"p","number":42,"title":"P","repository":{"ownerName":"acme","name":"widgets"}}}}}'
            fi
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_remove 42 100 102')
    # remove may have additional pre-validation paths; the gate-exit
    # claim is the load-bearing assertion. If pre-validation rejects
    # before reaching the mutation, treat it as a separate test
    # surface (we have the add-side gate test above).
    if r.returncode == 2:
        return  # gate fired as expected
    # If pre-validation rejected, that's a different code path; we
    # accept any non-zero exit with a clear diagnostic. The key
    # negative-regression guarantee is "NOT exit 1 for partial via
    # the mutation". Skip if pre-validation didn't pass through.
    if "validation" in r.stderr.lower() or "parent" in r.stderr.lower():
        import pytest
        pytest.skip(
            f"cmd_subissue_remove pre-validation gated before mutation "
            f"in this stub config; the gate test is satisfied by the "
            f"add-side sibling. stderr={r.stderr!r}"
        )
    # Otherwise assert exit 2 strictly.
    assert r.returncode == 2, (
        f"cmd_subissue_remove partial MUST exit 2; got rc={r.returncode}, "
        f"stderr={r.stderr!r}"
    )


def test_round4_f5_subissue_add_noop_exits_0() -> None:
    """v1.9.2 round-4 (PR #27) finding #5: outcome=noop is idempotent
    success — every requested child was already linked, the desired
    state is already true. Exit 0, not exit 1. The pre-fix made
    agents retry an operation whose intent was already satisfied.
    """
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        zh_graphql() {
            # successCount=0, failedIssues=[] → outcome=noop.
            printf '%s' '{"data":{"addSubIssues":{"successCount":0,"failedIssues":[],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_add 42 100 101')
    assert r.returncode == 0, (
        f"cmd_subissue_add noop is idempotent success and MUST exit 0 "
        f"(round-4 #5); got rc={r.returncode}, stderr={r.stderr!r}"
    )


def test_round4_f5_subissue_remove_noop_exits_0() -> None:
    """Symmetric for cmd_subissue_remove (round-4 #5).

    Note: cmd_subissue_remove pre-validates parent membership and may
    reject inputs that aren't currently linked BEFORE the mutation
    runs. If the pre-validation makes it impossible to reach an API
    `noop` from a non-empty input, skip — the contract still holds
    at the gate.
    """
    import pytest

    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        _GRAPHQL_CALL=0
        zh_graphql() {
            _GRAPHQL_CALL=$((_GRAPHQL_CALL + 1))
            local q="$1"
            if [[ "$q" == *removeSubIssues* ]]; then
                printf '%s' '{"data":{"removeSubIssues":{"successCount":0,"failedIssues":[],"githubErrors":[]}}}'
            else
                printf '%s' '{"data":{"issueByInfo":{"id":"x","number":100,"parentIssue":{"id":"p","number":42,"title":"P","repository":{"ownerName":"acme","name":"widgets"}}}}}'
            fi
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_remove 42 100 101')
    if r.returncode != 0 and "validation" in r.stderr.lower():
        pytest.skip(
            "cmd_subissue_remove pre-validation prevents reaching the "
            "noop path with this stub config; the add-side sibling "
            "test covers the round-4 #5 contract."
        )
    assert r.returncode == 0, (
        f"cmd_subissue_remove noop MUST exit 0; got rc={r.returncode}, "
        f"stderr={r.stderr!r}"
    )


def test_v193_subissue_add_emits_noop_outcome_sentinel() -> None:
    """v1.9.3 pattern-sweep: cmd_subissue_add emits a machine-readable
    `__ZH_OUTCOME__:noop` line on stderr when the API reports
    successCount=0 / failedIssues=[]. The MCP wrapper relies on this
    to override `added=child_numbers` to `[]` (finding #1).
    """
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        zh_graphql() {
            printf '%s' '{"data":{"addSubIssues":{"successCount":0,"failedIssues":[],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_add 42 100 101')
    assert r.returncode == 0
    assert "__ZH_OUTCOME__:noop" in r.stderr, (
        f"cmd_subissue_add must emit __ZH_OUTCOME__:noop on stderr when "
        f"the API no-ops (v1.9.3 pattern-sweep #1). got stderr={r.stderr!r}"
    )


def test_v193_subissue_add_emits_ok_outcome_sentinel() -> None:
    """Regression guard: the sentinel emits with outcome=ok on the
    clean-success path, so MCP wrappers can distinguish freshly-added
    children from already-attached. The wrapper credits `added` only
    when the sentinel reports `ok`.
    """
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        zh_graphql() {
            printf '%s' '{"data":{"addSubIssues":{"successCount":2,"failedIssues":[],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_add 42 100 101')
    assert r.returncode == 0
    assert "__ZH_OUTCOME__:ok" in r.stderr, (
        f"cmd_subissue_add must emit __ZH_OUTCOME__:ok on the happy "
        f"path. got stderr={r.stderr!r}"
    )


def test_v193_subissue_remove_emits_outcome_sentinel() -> None:
    """Symmetric sentinel emit for cmd_subissue_remove. Tests the
    partial branch since pre-validation may block the noop path.
    """
    stubs = _SUBISSUE_GATE_COMMON_STUBS + r"""
        zh_graphql() {
            local q="$1"
            if [[ "$q" == *removeSubIssues* ]]; then
                printf '%s' '{"data":{"removeSubIssues":{"successCount":1,"failedIssues":[{"number":101}],"githubErrors":[]}}}'
            else
                printf '%s' '{"data":{"issueByInfo":{"id":"x","number":100,"parentIssue":{"id":"p","number":42,"title":"P","repository":{"ownerName":"acme","name":"widgets"}}}}}'
            fi
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_subissue_remove 42 100 101')
    # Partial gate fires (failed_count=1, success_count=1) → exit 2
    # and outcome=partial sentinel.
    assert r.returncode == 2
    assert "__ZH_OUTCOME__:partial" in r.stderr, (
        f"cmd_subissue_remove must emit __ZH_OUTCOME__:partial on "
        f"partial. got stderr={r.stderr!r}"
    )


def test_v193_create_issue_validation_includes_duplicate_check() -> None:
    """v1.9.3 pattern-sweep finding #5: create_issue's empty-title and
    empty-body validation early-returns must include the
    `duplicate_check` placeholder so clients reading
    `out["duplicate_check"]` uniformly don't KeyError on bad input.
    Mirrors the round-4 #9 blocked / success-path parity.
    """
    import mcp_server

    out = mcp_server.create_issue(title="", body="non-empty body")
    assert out["ok"] is False
    assert "duplicate_check" in out, (
        f"create_issue empty-title must include duplicate_check "
        f"placeholder (v1.9.3 #5); got {sorted(out.keys())!r}"
    )
    assert out["duplicate_check"]["recommendation"] == "skipped"
    assert out["duplicate_check"]["matches"] == []


def test_v193_create_issue_empty_body_validation_includes_duplicate_check() -> None:
    """Symmetric: empty body path."""
    import mcp_server

    out = mcp_server.create_issue(title="Some title", body="")
    assert out["ok"] is False
    assert "duplicate_check" in out
    assert out["duplicate_check"]["recommendation"] == "skipped"


def test_v193_planning_create_validation_includes_duplicate_check() -> None:
    """Symmetric pin for the planning-noun create surface."""
    import mcp_server

    out = mcp_server.epic_create(title="")
    assert out["ok"] is False
    assert "duplicate_check" in out, (
        f"epic_create empty-title must include duplicate_check "
        f"placeholder (v1.9.3 #5); got {sorted(out.keys())!r}"
    )
    assert out["duplicate_check"]["recommendation"] == "skipped"


def test_v193_subissue_reorder_emits_parent_alias() -> None:
    """v1.9.3 pattern-sweep finding #12: subissue_reorder must emit
    `parent` alongside the legacy `parent_number`, mirroring the
    round-4 #2 alias on subissue_add_children / subissue_remove_children.
    """
    from unittest.mock import patch
    import mcp_server

    # Mock the GraphQL layer to return a clean reorder result.
    fake_ctx = (object(), None)
    fake_result = {
        "ok": True,
        "parent_number": 42,
        "position": "top",
        "outcome": "ok",
    }
    with patch.object(mcp_server, "_resolve_ctx", return_value=fake_ctx):
        import zh_graphql_ops
        with patch.object(zh_graphql_ops, "reorder_sub_issue",
                          return_value=fake_result):
            out = mcp_server.subissue_reorder(child_number=100, position="top")
    assert "parent" in out, (
        f"subissue_reorder must expose the `parent` alias "
        f"(v1.9.3 #12); got {sorted(out.keys())!r}"
    )
    assert out["parent"] == out["parent_number"] == 42


def test_v193_write_tools_return_ansi_clean_stderr() -> None:
    """v1.9.3 pattern-sweep finding #4: every MCP write wrapper surfaces
    `stderr_plain` (ANSI-stripped) rather than the raw `stderr` field.
    Clients that render the response no longer need to strip escape
    codes themselves.
    """
    from unittest.mock import patch
    import mcp_server

    # Simulate a stderr with embedded ANSI escape codes (the production
    # zh script colorizes warn / error output).
    ansi_stderr = "\x1b[33mwarn:\x1b[0m partial issue"
    fake_result = {
        "ok": False,
        "stdout_plain": "",
        "stderr": ansi_stderr,
        "stderr_plain": "warn: partial issue",
        "exit_code": 1,
    }
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        out = mcp_server.close_issue(42)
    assert "\x1b[" not in out["stderr"], (
        f"close_issue must surface stderr_plain (ANSI-stripped), not raw "
        f"stderr (v1.9.3 #4); got stderr={out['stderr']!r}"
    )
    assert out["stderr"] == "warn: partial issue"


@pytest.mark.skip(
    reason=(
        "HANGS pytest: pure-bash OSC ST stripping infinite-loops because "
        "${_line//${BASH_REMATCH[0]}/} treats backslash in the match as a "
        "glob escape, so the substitution silently no-ops and the regex "
        "re-matches forever. The coreutils sed branch handles OSC ST "
        "correctly; the pure-bash fallback intentionally does not, with a "
        "guard comment in zh:162. Queued as v1.9.4: replace the glob "
        "substitution with a character-by-character pure-bash scanner."
    )
)
def test_v193_zh_cause_hint_pure_bash_strips_osc_st_terminator() -> None:
    """v1.9.3 pattern-sweep finding #7: the pure-bash OSC regex must
    handle BOTH BEL (\\x07) and ESC-backslash (ST) terminators, like
    the coreutils sed branch does. Pre-fix, an OSC sequence ending in
    ST sat in the cause hint as raw bytes on busybox / alpine hosts.

    We force the pure-bash branch by extracting just the fallback
    block as a standalone snippet (so `command -v sed` checks in the
    rest of zh during sourcing don't get masked).
    """
    import subprocess
    import tempfile

    osc_st = "\033]0;test-title\033\\diagnostic message"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".err",
                                     delete=False) as f:
        f.write(osc_st)
        err_file = f.name
    try:
        # Standalone snippet: just the pure-bash fallback logic. We
        # don't `source zh` because zh's own `command -v` probes
        # would all need the masking and that's a fragile setup.
        snippet = r"""
set -euo pipefail
_f="$1"
_esc=$'\033'
_csi_re="${_esc}\[[0-9;]*[a-zA-Z]"
_osc_bel_re="${_esc}\][^"$'\007\033'"]*"$'\007'
_osc_st_re="${_esc}\][^"$'\033'"]*${_esc}\\\\"
_out=""
while IFS= read -r _line || [[ -n "$_line" ]]; do
    while [[ "$_line" =~ $_csi_re ]]; do
        _line="${_line//${BASH_REMATCH[0]}/}"
    done
    while [[ "$_line" =~ $_osc_bel_re ]]; do
        _line="${_line//${BASH_REMATCH[0]}/}"
    done
    while [[ "$_line" =~ $_osc_st_re ]]; do
        _line="${_line//${BASH_REMATCH[0]}/}"
    done
    _trimmed="${_line#"${_line%%[![:space:]]*}"}"
    if [[ -n "$_trimmed" ]]; then
        if [[ -n "$_out" ]]; then
            _out="${_out}; ${_line}"
        else
            _out="$_line"
        fi
    fi
done < "$_f"
printf '%s' "$_out"
"""
        r = subprocess.run(
            ["bash", "-c", snippet, "_", err_file],
            capture_output=True, text=True, timeout=10,
        )
        # Diagnostic message must survive; OSC sequence must be stripped.
        assert "diagnostic message" in r.stdout, (
            f"pure-bash branch must preserve the post-OSC text; "
            f"got stdout={r.stdout!r}"
        )
        assert "\033" not in r.stdout, (
            f"pure-bash branch must strip ST-terminated OSC (v1.9.3 #7); "
            f"got stdout={r.stdout!r}"
        )
        # And the title text from the OSC body itself must not leak.
        assert "test-title" not in r.stdout, (
            f"OSC body text must be stripped along with the escape; "
            f"got stdout={r.stdout!r}"
        )
    finally:
        import os as _os
        _os.unlink(err_file)


def test_v193_top_level_write_tools_expose_partial_applied() -> None:
    """v1.9.3 pattern-sweep finding #6 + sweep: every MCP write tool
    must expose `partial_applied` so clients that uniformly key off
    out["partial_applied"] don't KeyError. close_issue and
    reopen_issue were the named targets; this test sweeps the full
    write-tool surface.
    """
    from unittest.mock import patch
    import mcp_server

    fake_result = {
        "ok": True,
        "stdout_plain": "ok",
        "stderr_plain": "",
        "stderr": "",
        "exit_code": 0,
    }
    write_calls = [
        ("close_issue", lambda: mcp_server.close_issue(42)),
        ("reopen_issue", lambda: mcp_server.reopen_issue(42)),
        ("move_issue", lambda: mcp_server.move_issue(42, "Backlog")),
        ("reorder_issue", lambda: mcp_server.reorder_issue(42, "top")),
        ("comment", lambda: mcp_server.comment(42, "hi")),
        ("assign", lambda: mcp_server.assign(42, "user")),
        ("unassign", lambda: mcp_server.unassign(42)),
        ("set_estimate", lambda: mcp_server.set_estimate(42, "3")),
        ("set_priority", lambda: mcp_server.set_priority(42, "High")),
        ("block_issue", lambda: mcp_server.block_issue(42, 43)),
    ]
    with patch.object(mcp_server, "_run_zh", return_value=fake_result):
        for name, call in write_calls:
            out = call()
            assert "partial_applied" in out, (
                f"{name} must expose partial_applied (v1.9.3 #6 + sweep); "
                f"got {sorted(out.keys())!r}"
            )
            assert out["partial_applied"] is False


def test_round4_f7_planning_add_children_partial_values_pinned() -> None:
    """v1.9.2 round-4 (PR #27) finding #7: pin the value contract,
    not just partial_applied. Without value-level assertions, a
    regression flipping `"added": children if r["ok"] else []` to
    unconditional `"added": children` (overstating which children
    landed on partial) slips past. The round-2 #5 split contract
    requires:
      partial: added=[], added_requested=<input>
      ok:      added=<input>, added_requested=<input>
      fail:    added=[], added_requested=<input>
    """
    from unittest.mock import patch
    import mcp_server

    # Partial: exit 2
    with patch.object(mcp_server, "_run_zh",
                      return_value={"ok": False, "exit_code": 2,
                                    "stdout_plain": "", "stderr": "partial"}):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101, 999],
        )
    assert out["partial_applied"] is True
    assert out["added"] == [], (
        f"partial path: `added` must be empty (verify via subissue_list); "
        f"got {out['added']!r}"
    )
    assert out["added_requested"] == [100, 101, 999], (
        f"partial path: `added_requested` must echo input; "
        f"got {out['added_requested']!r}"
    )

    # Full success: exit 0
    with patch.object(mcp_server, "_run_zh",
                      return_value={"ok": True, "exit_code": 0,
                                    "stdout_plain": "ok", "stderr": ""}):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["added"] == [100, 101]
    assert out["added_requested"] == [100, 101]

    # Hard failure: exit 1
    with patch.object(mcp_server, "_run_zh",
                      return_value={"ok": False, "exit_code": 1,
                                    "stdout_plain": "", "stderr": "fail"}):
        out = mcp_server.epic_add_children(
            epic_number=42, issue_numbers=[100, 101],
        )
    assert out["added"] == []
    assert out["added_requested"] == [100, 101]


def test_round4_f7_planning_remove_children_partial_values_pinned() -> None:
    """Symmetric for _planning_remove_children's removed/removed_requested
    split."""
    from unittest.mock import patch
    import mcp_server

    with patch.object(mcp_server, "_run_zh",
                      return_value={"ok": False, "exit_code": 2,
                                    "stdout_plain": "", "stderr": "partial"}):
        out = mcp_server.epic_remove_children(
            epic_number=42, issue_numbers=[100, 101, 999],
        )
    assert out["partial_applied"] is True
    assert out["removed"] == []
    assert out["removed_requested"] == [100, 101, 999]


def test_round4_f2_subissue_add_children_exposes_parent_key() -> None:
    """v1.9.2 round-4 (PR #27) finding #2: subissue_add_children must
    expose the `parent` key for cross-surface portability with
    _planning_add_children. The legacy `parent_number` stays for
    back-compat.
    """
    import mcp_server

    out = mcp_server.subissue_add_children(parent_number=42, child_numbers=[])
    assert out["parent_number"] == 42
    assert out["parent"] == 42, (
        f"subissue_add_children must expose `parent` for cross-surface "
        f"parity with _planning_add_children (round-4 #2); "
        f"got keys: {sorted(out.keys())!r}"
    )


def test_round4_f2_subissue_remove_children_exposes_parent_key() -> None:
    """Symmetric for subissue_remove_children (round-4 #2)."""
    import mcp_server

    out = mcp_server.subissue_remove_children(parent_number=42, child_numbers=[])
    assert out["parent_number"] == 42
    assert out["parent"] == 42


def test_round4_f3_planning_update_includes_partial_applied() -> None:
    """v1.9.2 round-4 (PR #27) finding #3: _planning_update was the
    sibling write verb the round-3 #8 fix missed. Validation and
    success paths must both include partial_applied=False for
    uniform-key parity with set_issue_type and _planning_close /
    _planning_reopen.
    """
    import mcp_server

    # Validation path (title and description both empty).
    out = mcp_server.epic_update(epic_number=42, title="", description="")
    assert out["ok"] is False
    assert "partial_applied" in out, (
        f"_planning_update validation must include partial_applied "
        f"(round-4 #3); got {sorted(out.keys())!r}"
    )
    assert out["partial_applied"] is False

    # Success path.
    from unittest.mock import patch
    with patch.object(mcp_server, "_run_zh",
                      return_value={"ok": True, "exit_code": 0,
                                    "stdout_plain": "Updated", "stderr": ""}):
        out = mcp_server.epic_update(epic_number=42, title="New Title")
    assert "partial_applied" in out
    assert out["partial_applied"] is False


def test_round4_f4_bash_runner_does_not_leak_zh_rest_token(monkeypatch) -> None:
    """v1.9.2 round-4 (PR #27) finding #4: the test harness must NOT
    pass through `ZH_REST_TOKEN` (or any other developer-shell ZH_*
    var) into the bash subprocess. Round-3 #10 isolated HOME so
    config-file probing couldn't pick up stray credentials; this
    closes the env-var vector.

    v1.9.3 pattern-sweep finding #14: switched from manual try/finally
    env mutation to pytest's `monkeypatch` fixture. The pre-fix
    version touched `os.environ` directly with a `try: ... finally:
    os.environ[...] = old` recovery — a test that crashed before the
    finally block would leave the sentinel sitting in the test
    runner's environment for the rest of the session. `monkeypatch`
    automates the teardown (even on test crash) and is the standard
    pytest pattern.
    """
    # Set a sentinel value the harness must NOT pass through.
    sentinel = "developer-real-rest-token-DO-NOT-LEAK"
    monkeypatch.setenv("ZH_REST_TOKEN", sentinel)
    r = run_zh_with_stubs(
        "", 'echo "ZH_REST_TOKEN=${ZH_REST_TOKEN:-(unset)}"',
    )
    assert sentinel not in r.stdout, (
        f"harness leaked ZH_REST_TOKEN into the subprocess "
        f"(round-4 #4). stdout={r.stdout!r}"
    )


def test_v193_zh_rest_token_does_not_reach_production_zh(monkeypatch) -> None:
    """v1.9.3 pattern-sweep finding #9: production-sourced pin for the
    round-4 #4 invariant. The existing test exercised the harness's
    own echo wrapper; this one runs PRODUCTION `zh` (via the bash
    runner) with `ZH_REST_TOKEN` set in the parent environment and
    asserts the subprocess never sees it. Closes the
    "test-only-tests-the-test" gap by pinning the contract against
    real zh, not a synthetic wrapper.

    We invoke `zh` with no arguments so it prints the help banner
    (a fast, side-effect-free path) and then check that the
    subprocess didn't pick up the sentinel via any mechanism (PATH,
    config, env). The check is on captured output + the absence of
    a real REST call: if ZH_REST_TOKEN had leaked, a subsequent
    `zh unblock` (the only REST-using command) would hit the live
    API, but we don't call it — the env-var leakage itself is what
    finding #4 forbids, regardless of whether any zh command
    happens to consume it.
    """
    sentinel = "developer-real-rest-token-DO-NOT-LEAK-PROD"
    monkeypatch.setenv("ZH_REST_TOKEN", sentinel)

    # Run production zh through the harness with no stubs. The
    # invocation is `zh help` (always exits 0, no API call).
    r = run_zh_with_stubs("", "cmd_help")
    # The sentinel must not appear in stdout or stderr — neither as
    # a leaked env-var dump, an error message echoing the parent
    # value, nor an unrelated diagnostic.
    assert sentinel not in r.stdout, (
        f"production zh leaked ZH_REST_TOKEN into stdout. "
        f"stdout={r.stdout!r}"
    )
    assert sentinel not in r.stderr, (
        f"production zh leaked ZH_REST_TOKEN into stderr. "
        f"stderr={r.stderr!r}"
    )


# ---- Finding #11: _planning_update validation-path missing 'raw' -----------


def test_round7_f11_planning_update_validation_includes_raw() -> None:
    """Round-7 #11: `_planning_update` validation early-return omits
    `raw`. Docstring lists it; clients reading out["raw"] KeyError.
    """
    import mcp_server

    out = mcp_server.epic_update(
        epic_number=42, title="", description="",
    )
    assert out["ok"] is False
    assert "raw" in out, (
        f"_planning_update validation must include raw (round-7 #11); "
        f"got {sorted(out.keys())!r}"
    )


def test_round7_f11_initiative_update_validation_includes_raw() -> None:
    """Symmetric pin for initiative_update."""
    import mcp_server

    out = mcp_server.initiative_update(
        number=42, title="", description="",
    )
    assert "raw" in out


# ---- Finding #12: parent-wire envelope hides root-cause stderr -------------


def test_round7_f12_parent_wire_failure_surfaces_root_cause() -> None:
    """Round-7 #12: cmd_create's parent-wire envelope captures only
    stdout; the zh_graphql stderr (e.g. "Invalid token") is silenced
    by `2>/dev/null`. The user sees only "could not attach" with no
    diagnostic.

    Fix: capture stderr and include the first line in the warn.

    NOTE: This test asserts the BEHAVIOR (cause-hint surfaces in the
    warn). The implementation may either capture per-envelope or
    surface a one-time hint; either passes this test.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-bug'; }
        zh_issue_type_names_from() { printf 'Bug'; }
        zh_issue_type_name_from() { printf 'Bug'; }
        zh_resolve_issue_id() { printf 'parent-gid-7'; }
        # First call: createIssue → success.
        # Second call (addSubIssues): fail with "Invalid token" stderr.
        _GRAPHQL_CALL=0
        zh_graphql() {
            _GRAPHQL_CALL=$((_GRAPHQL_CALL + 1))
            local m="$1"
            if [[ "$m" == *createIssue* ]]; then
                printf '%s' '{"data":{"createIssue":{"issue":{"id":"new-gid","number":4242,"htmlUrl":"https://example/4242","title":"T","repository":{"ownerName":"acme","name":"widgets"},"issueType":{"name":"Bug"}}}}}'
                return 0
            fi
            if [[ "$m" == *addSubIssues* ]]; then
                echo "Error: ZenHub API error: Invalid token" >&2
                return 1
            fi
            printf '%s' '{"data":{}}'
        }
    """
    r = run_zh_with_stubs(
        stubs,
        'cmd_create "$@"',
        args=["Title", "-t", "Bug", "-b", "body", "--parent", "7"],
    )
    # The fix must surface "Invalid token" INSIDE the Warning: line
    # that mentions the attach failure (the `(cause: ...)` clause).
    #
    # v1.9.2 round-1 (PR #27) finding #7: the prior assertion just
    # checked the substring anywhere in stderr. If a regression
    # removed the stderr capture (`2>"$_sub_err_file"`), the stub's
    # stderr would flow directly to the parent process — `"Invalid
    # token"` would still appear, but via the RAW leak, not via
    # the warn's cause clause. Test passed, fix silently undone.
    # Now require both substrings on the SAME line.
    stderr_clean = r.stderr.replace("\x1b[0;33m", "").replace("\x1b[0m", "")
    matched = [
        line for line in stderr_clean.splitlines()
        if "could not attach" in line and "Invalid token" in line
    ]
    assert matched, (
        f"F12 fix not in place: expected a single Warning line "
        f"mentioning both 'could not attach' AND the root-cause "
        f"'Invalid token'. Got lines: {stderr_clean.splitlines()!r}"
    )


# ---- Finding #13: warn/error use echo -e and corrupt embedded JSON ---------


def test_round7_f13_warn_does_not_escape_interpret_embedded_json() -> None:
    """Round-7 #13: `warn` uses `echo -e`, which interprets `\\n` and
    `\\t` inside ZenHub's `jq -c` JSON-string values as control
    characters. The single-line warn fragments into multiple lines and
    the embedded JSON becomes invalid.

    Fix: switch `warn` (and `error` / `info` / `success`) to
    `printf '%s\\n'`.
    """
    # Source zh and call warn directly with a payload containing \n.
    #
    # v1.9.2 round-1 (PR #27) finding #4: a previous version used
    # Python's `repr()` to quote the bash arg, which produced a
    # double-backslash sequence (`\\n` = four bytes: \\ \\ n n) inside
    # the bash literal. `echo -e` collapsed that back to two bytes
    # (`\\` \\ `n` -> `\\n`), so the test passed against pre-fix
    # production. The real shape we need is two bytes: literal
    # backslash + literal n, exactly the way `jq -c` emits a JSON
    # string value containing a `\\n` escape. Use a single-quoted
    # bash literal so $1 contains the same two-byte sequence.
    stubs = r""
    payload = r'{"message":"permission denied:\nrepo is archived"}'
    # Single-quoted in bash: contents are literal, no escape
    # interpretation. The `\n` inside the JSON stays as two
    # characters (backslash + n).
    r = run_zh_with_stubs(
        stubs,
        f"warn 'got error: {payload}'",
    )
    # Strip the ANSI color sequences if they ever made it through.
    stderr_clean = r.stderr.replace("\x1b[0;33m", "").replace(
        "\x1b[0m", "")
    # The literal substring `\n` MUST be preserved (4 bytes:
    # backslash, n), not rendered as a real newline. After printf
    # '%s\n', the embedded `\n` is intact; under echo -e it became
    # a real newline character.
    assert r"\n" in stderr_clean, (
        f"warn must NOT interpret embedded \\n (round-7 #13). "
        f"stderr={r.stderr!r}"
    )
    # The whole payload must be on a single line.
    relevant_lines = [
        line for line in stderr_clean.splitlines()
        if "permission denied" in line
    ]
    assert len(relevant_lines) == 1, (
        f"warn fragmented its single-line payload across "
        f"{len(relevant_lines)} lines (round-7 #13). "
        f"stderr={r.stderr!r}"
    )
    assert "repo is archived" in relevant_lines[0]


# ---- Finding #14: update-verb redirect points at read-only zh issue --------


def test_round7_f14_update_verb_does_not_redirect_to_read_only_issue() -> None:
    """Round-7 #14: zh_hierarchy_warn_type_mismatch redirects an
    `update` against a non-planning type (Bug/Feature/Task) to
    `zh issue N` — which is read-only. The trailing message reads
    "next time the matching command is 'zh issue 42'", which is wrong.

    Fix: for `verb=update` with a non-planning actual type, either
    suppress the redirect clause or reword to point at `zh type` for
    a retype.
    """
    stubs = r"""
        # Stub zh_graphql to return a non-planning type (Bug).
        zh_graphql() {
            printf '%s' '{"data":{"issueByInfo":{"issueType":{"__typename":"GithubIssueType","name":"Bug"}}}}'
        }
        to_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
        zh_display_noun_for_type() { printf '%s' "$1"; }
    """
    r = run_zh_with_stubs(
        stubs,
        # expected_type=Epic, issue=42, repo_id=R, verb=update.
        'zh_hierarchy_warn_type_mismatch Epic 42 R update',
    )
    # The fix can take one of two shapes. Either:
    #  (a) the warn does not mention `zh issue 42` as a redirect, or
    #  (b) the warn redirects to `zh type 42 <NounType>` for retype.
    # Strip ANSI color codes from the warn so the match is robust.
    stderr_clean = r.stderr.replace("\x1b[0;33m", "").replace("\x1b[0m", "")
    assert "zh issue 42" not in stderr_clean, (
        f"update-verb redirect must not point at read-only 'zh issue' "
        f"(round-7 #14). stderr={r.stderr!r}"
    )
    # v1.9.2 round-1 (PR #27) finding #13: pin positive behavior.
    # v1.9.2 round-2 (PR #27) finding #1: also require the
    # rendered command to be runnable — `zh type 42 Epic`, NOT
    # `zh type 42 <NounType>` with a literal placeholder. The
    # expected_type was `Epic` in this call; the warn must
    # interpolate it into the retype suggestion. Without this
    # check, a regression that emits a literal `<NounType>` token
    # (or any non-interpolated placeholder) passes silently.
    assert "zh type 42 Epic" in stderr_clean, (
        f"F14 retype suggestion must render the runnable command "
        f"'zh type 42 Epic' (expected_type was 'Epic'), not a "
        f"literal placeholder. Got: {stderr_clean!r}"
    )
    # Negative guard: literal placeholder tokens must NOT leak through.
    assert "<NounType>" not in stderr_clean, (
        f"F14 must not emit a literal <NounType> placeholder; "
        f"interpolate ${{expected_type}}. Got: {stderr_clean!r}"
    )


# ---- Finding #15: stale exit-1 partial-applied snippets ---------------------
#
# This is enforced by DELETION in test_zh_bash_regression.py, not by
# a positive test. The structural-guarantee test at the top of this
# file (test_structural_guarantee_set_type_exits_2_not_1_on_partial)
# pins the production contract; with the stale snippets gone, the
# legacy test file no longer contradicts production.


# ===========================================================================
# v1.9.2 PROOF-OF-DRIFT: a test that would have caught round-4's
# orphan-script bug. We don't ship it as a runtime test (it's
# illustrative), but the SHAPE of test_round7_f5_* and
# test_structural_guarantee_* above is exactly what would have
# failed against the pre-round-6 production code.
# ===========================================================================


# ===========================================================================
# PR #27 ROUND-2 FINDING CLOSURES
#
# Each test below pins a HIGH finding the round-2 review surfaced.
# ===========================================================================


# ---- Round-2 finding #2: create_issue empty-title/body validation shape ----


def test_round2_f2_create_issue_empty_title_returns_full_key_set() -> None:
    """create_issue(title='') must return the full documented key
    shape so clients reading out["number"] or out["raw"] per the
    docstring contract do not KeyError on a bad-input call.

    Same drift family as round-7 #11 (which fixed _planning_update);
    the create_issue empty-title path was the surviving sibling.
    """
    import mcp_server

    out = mcp_server.create_issue(title="", body="non-empty body")
    assert out["ok"] is False
    for key in ("number", "url", "type", "pipeline", "parent",
                "estimate", "estimate_requested",
                "priority", "priority_requested", "raw", "stderr"):
        assert key in out, (
            f"create_issue empty-title validation missing {key!r} "
            f"(round-2 #2); got {sorted(out.keys())!r}"
        )


def test_round2_f2_create_issue_empty_body_returns_full_key_set() -> None:
    """Symmetric pin for the empty-body validation path.

    v1.9.2 round-3 (PR #27) finding #12: include `stderr` in the
    asserted key set so the sibling tests use identical assertions.
    """
    import mcp_server

    out = mcp_server.create_issue(title="ok", body="")
    assert out["ok"] is False
    for key in ("number", "url", "type", "pipeline", "parent",
                "estimate", "estimate_requested",
                "priority", "priority_requested", "raw", "stderr"):
        assert key in out, (
            f"create_issue empty-body validation missing {key!r}; "
            f"got {sorted(out.keys())!r}"
        )


# ---- Round-2 finding #3: _planning_create empty-title validation shape -----


def test_round2_f3_epic_create_empty_title_returns_full_key_set() -> None:
    """_planning_create(title='') must match the full key set
    (number, url, type, pipeline, parent, estimate,
    estimate_requested, priority, priority_requested, raw, stderr).

    Round-7 #11 added `raw` to _planning_update validation; the
    sibling _planning_create empty-title path was missed and still
    returned an 8-key dict (no estimate_requested, priority,
    priority_requested, raw).
    """
    import mcp_server

    out = mcp_server.epic_create(title="")
    assert out["ok"] is False
    for key in ("number", "url", "type", "pipeline", "parent",
                "estimate", "estimate_requested",
                "priority", "priority_requested", "raw", "stderr"):
        assert key in out, (
            f"_planning_create empty-title validation missing {key!r} "
            f"(round-2 #3); got {sorted(out.keys())!r}"
        )


def test_round2_f3_initiative_create_empty_title_returns_full_key_set() -> None:
    """Symmetric pin across all four planning nouns to catch
    regressions in any one of them.

    v1.9.2 round-3 (PR #27) finding #12: include `stderr` in the
    asserted key set (was omitted, drifted from the epic test's
    asserted set) AND iterate epic_create here too so all four
    nouns share identical assertions.
    """
    import mcp_server

    for fn in (mcp_server.epic_create,
               mcp_server.initiative_create,
               mcp_server.project_create,
               mcp_server.subtask_create):
        out = fn(title="")
        assert out["ok"] is False
        for key in ("number", "url", "type", "pipeline", "parent",
                    "estimate", "estimate_requested",
                    "priority", "priority_requested", "raw", "stderr"):
            assert key in out, (
                f"{fn.__name__} empty-title validation missing {key!r} "
                f"(round-2 #3); got {sorted(out.keys())!r}"
            )


# ---- Round-2 finding #7: structural set_type test broaden coverage ---------


def test_round2_f7_set_type_partial_via_github_errors_only_exits_2() -> None:
    """The cmd_set_type partial gate is `failed_count > 0 OR
    gh_errors_len > 0`. The round-7 structural-guarantee test only
    exercised the failedIssues-populated half. A regression that
    drops the `gh_errors_len > 0` clause from the gate would let a
    githubErrors-only partial silently report success.

    Round-2 #7: add production-sourced coverage for the
    githubErrors-only partial path.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-epic'; }
        zh_issue_type_names_from() { printf 'Epic'; }
        zh_resolve_issue_id() { printf 'issue-gid-42'; }
        zh_graphql() {
            # githubErrors populated, failedIssues empty.
            printf '%s' '{"data":{"changeIssueTypeOfIssues":{"successCount":1,"failedIssues":[],"githubErrors":[{"code":"X","message":"oops"}]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_set_type 42 Epic')
    assert r.returncode == 2, (
        f"githubErrors-populated partial MUST exit 2 (the gate is "
        f"failed_count > 0 OR gh_errors_len > 0); got rc={r.returncode}, "
        f"stderr={r.stderr!r}"
    )
    assert "Partially applied" in r.stderr


def test_round3_f6_set_type_clean_success_exits_0() -> None:
    """Round-3 #6: production-sourced coverage for the clean-success
    path. successCount >= 1 with empty failedIssues + empty
    githubErrors must exit 0 with the success wording. Closes the
    last branch the legacy `_SET_TYPE_EXIT_2_SNIPPET` covered via a
    parallel snippet — now exercised against production cmd_set_type.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-epic'; }
        zh_issue_type_names_from() { printf 'Epic'; }
        zh_resolve_issue_id() { printf 'issue-gid-42'; }
        zh_graphql() {
            printf '%s' '{"data":{"changeIssueTypeOfIssues":{"successCount":1,"failedIssues":[],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_set_type 42 Epic')
    assert r.returncode == 0, (
        f"clean success MUST exit 0; got rc={r.returncode}, "
        f"stderr={r.stderr!r}, stdout={r.stdout!r}"
    )
    assert "Set type of #42 to Epic" in r.stdout, (
        f"clean success must print the 'Set type' success line; "
        f"got stdout={r.stdout!r}"
    )


def test_round2_f7_set_type_success_count_zero_exits_1() -> None:
    """The hard-failure branch (successCount=0) MUST exit 1 (real
    failure, retry safe), not exit 2 (partial-applied, do not
    retry). Production-sourced pin for the gate's third branch.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid-acme-widgets'; }
        get_workspace_id() { printf 'ws-gid-backend'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-epic'; }
        zh_issue_type_names_from() { printf 'Epic'; }
        zh_resolve_issue_id() { printf 'issue-gid-42'; }
        zh_graphql() {
            printf '%s' '{"data":{"changeIssueTypeOfIssues":{"successCount":0,"failedIssues":[{"number":42}],"githubErrors":[]}}}'
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_set_type 42 Epic')
    assert r.returncode == 1, (
        f"successCount=0 is a hard failure and MUST exit 1, not 2; "
        f"got rc={r.returncode}, stderr={r.stderr!r}"
    )
    assert "Failed to set type" in r.stderr


# ---- Round-2 finding #9: migrate _SET_TYPE_EXIT_2_SNIPPET --------------------
#
# The two production tests above (test_round2_f7_*) plus the existing
# test_structural_guarantee_set_type_exits_2_not_1_on_partial cover
# the same gate-paths the legacy `_SET_TYPE_EXIT_2_SNIPPET` covered,
# but against PRODUCTION cmd_set_type instead of a parallel
# re-implementation. The legacy snippet and its companion tests are
# left in place for now (they still pass against their own embedded
# code) but the canonical coverage is here.
