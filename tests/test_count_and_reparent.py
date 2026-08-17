"""Regression tests for silent truncation (#86) and reparenting (#85).

#86: listings issued a single `first: 50` page and then derived the displayed
count from `nodes | length` — so a 125-issue pipeline rendered "(50 issues)"
with nothing to indicate the number was a page rather than the set. A count that
is confidently wrong is worse than an error, because an error gets noticed.

#85: ZenHub has no reparent mutation and a child may have only one parent, so
moving children between parents failed as a bulk add with "Sub issue may only
have one parent" — naming no parent, and frequently blocked by a CLOSED issue
absent from every listing the caller would check.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server  # noqa: E402
from _bash_runner import run_zh_with_stubs  # noqa: E402


# --------------------------------------------------------------------------
# #86 — exact counts / no silent truncation
# --------------------------------------------------------------------------

# Two pages of issues (2 then 1), with the connection reporting totalCount=125.
# The old code would have reported 2 (one page's nodes); the fix must both walk
# page 2 AND surface 125 as the authoritative total.
_PAGINATED_PIPELINE = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_repo_id() { printf 'repo-gid'; }
    get_workspace_id() { printf 'ws-gid'; }
    zh_graphql() {
        local q="$1" v="$2"
        if [[ "$q" == *pipelinesConnection* ]]; then
            printf '%s' '{"data":{"workspace":{"pipelinesConnection":{"nodes":[{"id":"p1","name":"Product Backlog"}]}}}}'
            return 0
        fi
        if [[ "$q" == *searchIssuesByPipeline* ]]; then
            if printf '%s' "$v" | grep -q 'CUR1'; then
                printf '%s' '{"data":{"searchIssuesByPipeline":{"totalCount":125,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"number":3,"title":"three","state":"OPEN","zenhubUrl":"u","estimate":null,"assignees":{"nodes":[]},"repository":{"ownerName":"acme","name":"widgets"}}]}}}'
            else
                printf '%s' '{"data":{"searchIssuesByPipeline":{"totalCount":125,"pageInfo":{"hasNextPage":true,"endCursor":"CUR1"},"nodes":[{"number":1,"title":"one","state":"OPEN","zenhubUrl":"u","estimate":null,"assignees":{"nodes":[]},"repository":{"ownerName":"acme","name":"widgets"}},{"number":2,"title":"two","state":"OPEN","zenhubUrl":"u","estimate":null,"assignees":{"nodes":[]},"repository":{"ownerName":"acme","name":"widgets"}}]}}}'
            fi
            return 0
        fi
        printf '%s' '{"data":{}}'
    }
"""


def test_pipeline_fetch_walks_every_page() -> None:
    """The helper must follow pageInfo, not stop at the first page."""
    r = run_zh_with_stubs(
        _PAGINATED_PIPELINE,
        'zh_pipeline_issues_fetch_all p1 ws-gid "{}" | jq -c "{n: (.nodes|length), total: .totalCount, complete: .complete}"',
    )
    assert r.returncode == 0, r.stderr
    assert '"n":3' in r.stdout, f"should have walked both pages; got {r.stdout!r}"
    assert '"total":125' in r.stdout, "totalCount must come from the connection, not the page"
    assert '"complete":true' in r.stdout


def test_pipeline_reports_authoritative_total_not_page_length() -> None:
    """THE #86 regression: the header must not present a page as the set.

    Only 3 of 125 are fetched here, so the output must say so rather than
    printing a bare, confident '3 issues'.
    """
    r = run_zh_with_stubs(_PAGINATED_PIPELINE, 'cmd_pipeline "Product Backlog"')
    assert r.returncode == 0, r.stderr
    assert "showing 3 of 125" in r.stdout, (
        f"a partial fetch must disclose the true total; got {r.stdout!r}"
    )


def test_pipeline_no_truncation_note_when_complete() -> None:
    """When we really did fetch everything, don't cry wolf."""
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_workspace_id() { printf 'ws-gid'; }
        zh_graphql() {
            local q="$1"
            if [[ "$q" == *pipelinesConnection* ]]; then
                printf '%s' '{"data":{"workspace":{"pipelinesConnection":{"nodes":[{"id":"p1","name":"Done"}]}}}}'
            elif [[ "$q" == *searchIssuesByPipeline* ]]; then
                printf '%s' '{"data":{"searchIssuesByPipeline":{"totalCount":1,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"number":7,"title":"seven","state":"OPEN","zenhubUrl":"u","estimate":null,"assignees":{"nodes":[]},"repository":{"ownerName":"acme","name":"widgets"}}]}}}'
            else printf '%s' '{"data":{}}'; fi
        }
    """
    r = run_zh_with_stubs(stubs, 'cmd_pipeline "Done"')
    assert r.returncode == 0, r.stderr
    assert "showing" not in r.stdout, f"complete fetch must not claim truncation; got {r.stdout!r}"


_COUNT_STUBS = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_workspace_id() { printf 'ws-gid'; }
    zh_graphql() {
        printf '%s' '{"data":{"workspace":{"pipelinesConnection":{"nodes":[{"name":"Product Backlog","issues":{"totalCount":125}},{"name":"In Progress","issues":{"totalCount":4}}]}}}}'
    }
"""


def test_count_totals_are_exact() -> None:
    r = run_zh_with_stubs(_COUNT_STUBS, "cmd_count -q")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "129", f"expected 125+4; got {r.stdout!r}"


def test_count_single_pipeline() -> None:
    r = run_zh_with_stubs(_COUNT_STUBS, 'cmd_count "Product Backlog" -q')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "125"


def test_count_json_declares_exactness() -> None:
    r = run_zh_with_stubs(_COUNT_STUBS, "cmd_count --json")
    assert r.returncode == 0, r.stderr
    assert '"exact":true' in r.stdout.replace(" ", "")
    assert '"total":129' in r.stdout.replace(" ", "")


def test_count_unknown_pipeline_errors_rather_than_zero() -> None:
    """A wrong name must not quietly answer 0 — that's the same class of lie."""
    r = run_zh_with_stubs(_COUNT_STUBS, 'cmd_count "Nonexistent"')
    assert r.returncode != 0
    assert "not found" in r.stderr.lower()


# --------------------------------------------------------------------------
# #85 — reparent
# --------------------------------------------------------------------------

# #60 lives under CLOSED #501; #73 has no parent; #99 is already under #586.
_REPARENT_STUBS = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_repo_id() { printf 'repo-gid'; }
    get_workspace_id() { printf 'ws-gid'; }
    zh_github_issue_state() { printf '%s' "${ZH_TEST_GH_STATE:-}"; }
    zh_graphql() {
        local q="$1" v="$2"
        if [[ "$q" == *removeSubIssues* ]]; then
            echo "DETACHED=$(printf '%s' "$v" | jq -c '.input.childIssueIds|sort')" >&2
            printf '%s' '{"data":{"removeSubIssues":{"clientMutationId":null}}}'
            return 0
        fi
        if [[ "$q" == *addSubIssues* ]]; then
            echo "ATTACHED=$(printf '%s' "$v" | jq -c '.input.childIssueIds|sort') PARENT=$(printf '%s' "$v" | jq -r '.input.parentId')" >&2
            printf '%s' '{"data":{"addSubIssues":{"successCount":9,"githubErrors":[]}}}'
            return 0
        fi
        local n
        n=$(printf '%s' "$v" | jq -r '.issueNumber // empty')
        case "$n" in
            586) printf '%s' '{"data":{"issueByInfo":{"id":"gid-586","number":586,"title":"Initiative","state":"OPEN","parentIssue":null}}}' ;;
            60)  printf '%s' '{"data":{"issueByInfo":{"id":"gid-60","number":60,"title":"Epic A","state":"OPEN","parentIssue":{"id":"gid-501","number":501,"title":"Old Project","state":"CLOSED"}}}}' ;;
            73)  printf '%s' '{"data":{"issueByInfo":{"id":"gid-73","number":73,"title":"Epic B","state":"OPEN","parentIssue":null}}}' ;;
            99)  printf '%s' '{"data":{"issueByInfo":{"id":"gid-99","number":99,"title":"Epic C","state":"OPEN","parentIssue":{"id":"gid-586","number":586,"title":"Initiative","state":"OPEN"}}}}' ;;
            *)   printf '%s' '{"data":{"issueByInfo":null}}' ;;
        esac
    }
"""


def test_reparent_dry_run_changes_nothing() -> None:
    """Dry run must print the plan and fire no mutation."""
    r = run_zh_with_stubs(_REPARENT_STUBS, "cmd_reparent 586 60 73 --dry-run")
    assert r.returncode == 0, r.stderr
    assert "detach from #501 (CLOSED)" in r.stdout, f"must name the blocking closed parent; got {r.stdout!r}"
    assert "no current parent" in r.stdout
    assert "DETACHED=" not in r.stderr, "dry run must not detach"
    assert "ATTACHED=" not in r.stderr, "dry run must not attach"


def test_reparent_detaches_only_children_that_have_a_parent() -> None:
    """#60 (has a parent) is detached; #73 (parentless) is not, but both attach."""
    r = run_zh_with_stubs(_REPARENT_STUBS, "cmd_reparent 586 60 73")
    assert r.returncode == 0, r.stderr
    assert 'DETACHED=["gid-60"]' in r.stderr, f"only #60 needs detaching; got {r.stderr!r}"
    assert 'ATTACHED=["gid-60","gid-73"]' in r.stderr
    assert "PARENT=gid-586" in r.stderr


def test_reparent_skips_children_already_under_destination() -> None:
    """#99 is already under #586 — it must be left completely alone."""
    r = run_zh_with_stubs(_REPARENT_STUBS, "cmd_reparent 586 99")
    assert r.returncode == 0, r.stderr
    assert "already a sub-issue of #586" in r.stdout
    assert "DETACHED=" not in r.stderr
    assert "ATTACHED=" not in r.stderr


def test_reparent_rejects_self_parent() -> None:
    r = run_zh_with_stubs(_REPARENT_STUBS, "cmd_reparent 586 586")
    assert r.returncode != 0
    assert "sub-issue of itself" in r.stderr


def test_reparent_unknown_destination_errors_before_mutating() -> None:
    r = run_zh_with_stubs(_REPARENT_STUBS, "cmd_reparent 4242 60")
    assert r.returncode != 0
    assert "not found" in r.stderr
    assert "DETACHED=" not in r.stderr


_CLOSED_DESTINATION_STUBS = _REPARENT_STUBS.replace(
    '"id":"gid-586","number":586,"title":"Initiative","state":"OPEN","parentIssue":null',
    '"id":"gid-586","number":586,"title":"Initiative","state":"CLOSED","parentIssue":null',
)


def test_reparent_refuses_closed_destination() -> None:
    """#92: reparent is the verb `zh doctor` points at for FIXING closed-parent
    orphans, so moving children onto another closed parent just relocates the
    defect. Refusal replaced the pre-#92 warn because the warn let the attach
    succeed, which is the entire failure mode: nobody reads a warning attached
    to a successful command.
    """
    r = run_zh_with_stubs(_CLOSED_DESTINATION_STUBS, "cmd_reparent 586 73 --dry-run")
    assert r.returncode != 0, f"a closed destination must be refused; got rc=0\n{r.stdout}"
    assert "CLOSED" in r.stderr and "roll up to nothing" in r.stderr
    # The guard fires BEFORE the plan is built, so nothing implies the move
    # would have worked and no mutation is reachable.
    assert "DETACHED=" not in r.stderr and "ATTACHED=" not in r.stderr


def test_reparent_closed_destination_hint_is_copy_pasteable() -> None:
    """The override the refusal suggests must run as-is (cf. the #85 hint bug)."""
    r = run_zh_with_stubs(_CLOSED_DESTINATION_STUBS, "cmd_reparent 586 73 60 --dry-run")
    hint = next(
        (ln.strip() for ln in r.stderr.splitlines() if "--allow-closed-parent" in ln), ""
    )
    assert hint == "zh reparent 586 73 60 --allow-closed-parent", (
        f"override hint must be runnable as-is; got {hint!r}"
    )


def test_reparent_allow_closed_parent_overrides_and_warns() -> None:
    """The override proceeds, but must still say what it is doing."""
    r = run_zh_with_stubs(
        _CLOSED_DESTINATION_STUBS,
        "cmd_reparent 586 73 --dry-run --allow-closed-parent",
    )
    assert r.returncode == 0, r.stderr
    assert "roll up to nothing" in r.stderr, "override must still warn"
    assert "__ZH_BLOCKED__" not in r.stderr, "an override is not a refusal"
    assert "attach to #586" in r.stdout, f"the plan must still be produced; got {r.stdout!r}"


# --------------------------------------------------------------------------
# #85 — doctor
# --------------------------------------------------------------------------

_DOCTOR_STUBS = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_workspace_id() { printf 'ws-gid'; }
    zh_graphql() {
        printf '%s' '{"data":{"workspace":{"issues":{"totalCount":4,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
            {"number":1,"title":"root","state":"OPEN","parentIssue":null},
            {"number":3,"title":"orphaned","state":"OPEN","parentIssue":{"number":9,"title":"dead project","state":"CLOSED"}},
            {"number":4,"title":"cyc-a","state":"OPEN","parentIssue":{"number":5,"title":"cyc-b","state":"OPEN"}},
            {"number":5,"title":"cyc-b","state":"OPEN","parentIssue":{"number":4,"title":"cyc-a","state":"OPEN"}}
        ]}}}}'
    }
    zh_github_issue_states_batch() { printf '%s' "${ZH_TEST_GH_BATCH:-{\"ok\":false,\"truncated\":false,\"queried\":0,\"states\":[]}}"; }
"""


def test_doctor_flags_open_issue_under_closed_parent() -> None:
    """The silent-orphan case: healthy-looking issue rolling up to nothing."""
    r = run_zh_with_stubs(_DOCTOR_STUBS, "cmd_doctor")
    assert r.returncode == 1, "doctor must exit 1 when it finds problems"
    assert "#3" in r.stdout and "parent #9 (CLOSED)" in r.stdout
    assert "zh reparent" in r.stdout, "should tell the operator how to fix it"


def test_doctor_detects_cycle_without_hanging() -> None:
    """Regression: the cycle walk must TERMINATE.

    The first implementation used `recurse(f) | select(. != null)` inside
    limit(); on a cycle the tail is discarded forever, limit() never fills, and
    the check hangs instead of reporting. run_zh_with_stubs would time out.
    """
    r = run_zh_with_stubs(_DOCTOR_STUBS, "cmd_doctor")
    assert r.returncode == 1
    assert "parent cycle" in r.stdout.lower()
    assert "#4" in r.stdout and "#5" in r.stdout


def test_doctor_json_is_pure_json_on_stdout() -> None:
    """Regression: `info()` writes to STDOUT, so an unguarded progress line
    corrupted the --json payload and `zh doctor --json | jq` failed outright."""
    import json as _json

    r = run_zh_with_stubs(_DOCTOR_STUBS, "cmd_doctor --json")
    assert r.returncode == 1, "findings still exit 1"
    parsed = _json.loads(r.stdout)  # must parse with NO stripping
    assert parsed["ok"] is False
    assert parsed["closed_parent_orphans"][0]["number"] == 3


def test_doctor_clean_workspace_passes() -> None:
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_workspace_id() { printf 'ws-gid'; }
        zh_graphql() {
            printf '%s' '{"data":{"workspace":{"issues":{"totalCount":2,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
                {"number":1,"title":"root","state":"OPEN","parentIssue":null},
                {"number":2,"title":"child","state":"OPEN","parentIssue":{"number":1,"title":"root","state":"OPEN"}}
            ]}}}}'
        }
        zh_github_issue_states_batch() { printf '%s' "${ZH_TEST_GH_BATCH:-{\"ok\":false,\"truncated\":false,\"queried\":0,\"states\":[]}}"; }
    """
    r = run_zh_with_stubs(stubs, "cmd_doctor")
    assert r.returncode == 0, r.stderr
    # The stub's mirror lookup fails, so the honest claim is the narrower one:
    # no problems found, states unverified. Claiming "healthy" here would
    # contradict the "not cross-checked" line printed just above it (#94).
    assert "No problems found" in r.stdout
    assert "not cross-checked" in r.stdout


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

def _capture(monkeypatch, stdout="", ok=True):
    calls = []

    def fake_run_zh(args, **kwargs):
        calls.append(list(args))
        return {"ok": ok, "stdout_plain": stdout, "stderr_plain": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    return calls


def test_mcp_move_children_forwards_and_flags_dry_run(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.move_children(to=586, issue_numbers=[60, 73], dry_run=True)
    assert calls == [["reparent", "586", "60", "73", "--dry-run"]]
    assert out["dry_run"] is True and out["to"] == 586


def test_mcp_move_children_empty_list_does_not_invoke_zh(monkeypatch):
    calls = _capture(monkeypatch)
    out = mcp_server.move_children(to=586, issue_numbers=[])
    assert out["ok"] is False
    assert calls == []


def test_one_parent_error_hint_is_copy_pasteable() -> None:
    """A suggested fix must actually run.

    The hint was built from the comma-separated human list ("#60, #72"), so it
    printed `zh reparent 42 60, 72` — which dies on reparent's own numeric guard.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        get_workspace_id() { printf 'ws-gid'; }
        zh_resolve_issue_ids() { printf '%s' '["gid-60","gid-72"]'; }
        zh_github_issue_state() { printf '%s' "${ZH_TEST_GH_STATE:-}"; }
        zh_graphql() {
            local q="$1"
            if [[ "$q" == *addSubIssues* ]]; then
                printf '%s' '{"data":{"addSubIssues":{"successCount":0,"githubErrors":["Sub issue may only have one parent"],"failedIssues":[{"number":60},{"number":72}]}}}'
            else
                printf '%s' '{"data":{"issueByInfo":{"id":"gid-x","number":1,"title":"t","state":"OPEN","parentIssue":{"id":"p","number":501,"title":"old","state":"CLOSED"}}}}'
            fi
        }
    """
    r = run_zh_with_stubs(stubs, "cmd_subissue_add 42 60 72 || true")
    out = r.stdout + r.stderr
    hint = next((ln for ln in out.splitlines() if "To move them anyway" in ln), "")
    assert hint, f"expected a reparent hint; got {out!r}"
    assert "zh reparent 42 60 72" in hint, f"hint must be runnable as-is; got {hint!r}"
    # The comma form is fine in the human "2 failed: #60, #72" line — but the
    # suggested COMMAND must carry bare space-separated numbers.
    assert "," not in hint.split("zh reparent")[-1], f"suggested command has comma args: {hint!r}"
    assert "#" not in hint.split("zh reparent")[-1], f"suggested command has # prefixes: {hint!r}"


def test_mcp_move_children_partial_detected_from_stderr(monkeypatch):
    """Regression: the partial marker is warn()->STDERR, so matching stdout made
    partial_applied dead code — it stayed False in the exact
    detach-succeeded/attach-partially-failed case it exists to flag."""
    def fake_run_zh(args, **kwargs):
        return {"ok": False,
                "stdout_plain": "Attaching 2 issue(s) to #586...",
                "stderr_plain": "Warning: Attached 1/2 to #586"}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    out = mcp_server.move_children(to=586, issue_numbers=[60, 73])
    assert out["ok"] is False
    assert out["partial_applied"] is True, "must flag the partial detach/attach state"


def test_mcp_count_parses_exact_total(monkeypatch):
    _capture(monkeypatch, stdout='{"total":125,"includes_closed":false,"exact":true,"pipelines":[]}')
    out = mcp_server.count(pipeline_name="Product Backlog")
    assert out["total"] == 125 and out["exact"] is True


def test_mcp_doctor_findings_are_not_tool_failure(monkeypatch):
    """zh doctor exits 1 on findings; that's a RESULT, not a broken tool."""
    _capture(monkeypatch, ok=False,
             stdout='{"checked":4,"open":4,"complete":true,"ok":false,'
                    '"closed_parent_orphans":[{"number":3,"parent":9}],"parent_cycles":[]}')
    out = mcp_server.doctor()
    assert out["ok"] is True, "the check ran, so ok=True"
    assert out["healthy"] is False, "but the hierarchy is unhealthy"
    assert out["closed_parent_orphans"][0]["number"] == 3
