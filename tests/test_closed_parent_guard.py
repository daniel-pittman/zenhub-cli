"""Regression tests for the closed-parent guard (#92).

Closing a parent does not detach its children. Anything wired to a closed
container therefore drops out of every container-level rollup while each issue
still looks perfectly healthy on its own, and no listing surfaces the condition
afterwards, because every listing that would reveal it is one the closed parent
is absent from.

Before #92, `zh doctor` detected this correctly but only after the fact. Nothing
warned at attach time and nothing marked the state on read, so the orphan was
created silently and found weeks later.

Two claims are pinned here, one per direction:

  * PREVENTION: every verb that SETS a parent (`zh create --parent`,
    `zh subissue add`, `zh <noun> add`, `zh reparent`) refuses a CLOSED parent
    and mutates nothing, with `--allow-closed-parent` as the deliberate
    override. The refusal tests assert the MUTATION DID NOT FIRE, not merely
    that a message was printed: a guard that prints and then attaches anyway is
    the exact defect #92 exists to close.
  * VISIBILITY: every verb that READS a parent (`zh issue`,
    `zh subissue list`) marks a closed one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server  # noqa: E402
import zh_graphql_ops  # noqa: E402
from _bash_runner import run_zh_with_stubs  # noqa: E402
from _fixtures import (  # noqa: E402
    add_sub_issues_response,
    issue_by_info_response,
    make_ctx,
    patch_ctx_query,
)


# ==========================================================================
# Bash: zh subissue add / zh <noun> add
# ==========================================================================

def _subissue_add_stubs(parent_state: str = "CLOSED") -> str:
    """Stubs for cmd_subissue_add against parent #42 and child #100.

    The addSubIssues arm appends to `$ZH_TEST_MUTATION_LOG`. A FILE, not a
    stderr marker: cmd_subissue_add wraps the mutation in a fail-soft envelope
    that redirects zh_graphql's stderr to a temp file, so a `>&2` marker would
    be swallowed and every "the mutation did not fire" assertion would pass
    vacuously. The log is the load-bearing evidence in every refusal test
    below; without it a guard that warns and then attaches anyway would look
    identical to one that refuses.
    """
    return r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        zh_github_issue_state() { printf '%s' "${ZH_TEST_GH_STATE:-}"; }
        zh_graphql() {
            local q="$1" v="$2"
            if [[ "$q" == *addSubIssues* ]]; then
                echo "MUTATION_FIRED" >> "$ZH_TEST_MUTATION_LOG"
                printf '%s' '{"data":{"addSubIssues":{"successCount":1,"failedIssues":[],"githubErrors":[]}}}'
                return 0
            fi
            local n
            n=$(printf '%s' "$v" | jq -r '.issueNumber // empty')
            case "$n" in
                42)  printf '%s' '{"data":{"issueByInfo":{"id":"gid-42","number":42,"title":"Retired container","state":"__PARENT_STATE__","parentIssue":null}}}' ;;
                100) printf '%s' '{"data":{"issueByInfo":{"id":"gid-100","number":100,"title":"Live work","state":"OPEN","parentIssue":null}}}' ;;
                *)   printf '%s' '{"data":{"issueByInfo":null}}' ;;
            esac
        }
    """.replace("__PARENT_STATE__", parent_state)


def _run_add(tmp_path, invocation: str, parent_state: str = "CLOSED",
             stubs: str | None = None):
    """Run an add-path invocation with a fresh mutation log.

    Returns (CompletedProcess, mutation_fired: bool). The log is created empty
    up front so a missing file can only mean the stub never appended, never
    that the harness failed to wire the path through.
    """
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        stubs if stubs is not None else _subissue_add_stubs(parent_state),
        invocation,
        extra_env={"ZH_TEST_MUTATION_LOG": str(log)},
    )
    return r, "MUTATION_FIRED" in log.read_text()


def test_subissue_add_refuses_closed_parent_without_mutating(tmp_path) -> None:
    """THE #92 regression: the attach that silently orphaned live work."""
    r, fired = _run_add(tmp_path, "cmd_subissue_add 42 100")
    assert r.returncode != 0, f"attaching to a closed parent must fail; got rc=0\n{r.stdout}"
    assert "is CLOSED" in r.stderr, f"the refusal must say why; got {r.stderr!r}"
    assert not fired, (
        "the guard printed a message but the addSubIssues mutation still ran. "
        "the orphan would have been created anyway"
    )


def test_subissue_add_refusal_does_not_announce_the_work(tmp_path) -> None:
    """The guard runs before the 'Adding N sub-issue(s)' line.

    A refusal that first announces the attach reads as a failure mid-operation
    rather than a decision not to start, which is the difference between "did
    something land?" and "nothing happened."
    """
    r, _ = _run_add(tmp_path, "cmd_subissue_add 42 100")
    assert "Adding 1 sub-issue" not in r.stdout, (
        f"refusal announced work it then declined; got {r.stdout!r}"
    )


def test_subissue_add_emits_blocked_sentinel(tmp_path) -> None:
    """The MCP wrappers key `blocked_closed_parent` off this line."""
    r, _ = _run_add(tmp_path, "cmd_subissue_add 42 100")
    assert any(
        ln.strip() == "__ZH_BLOCKED__:closed_parent" for ln in r.stderr.splitlines()
    ), f"missing the machine-readable refusal marker; got {r.stderr!r}"


def test_subissue_add_refusal_hint_is_copy_pasteable(tmp_path) -> None:
    """The suggested override must run as-is (cf. the #85 hint bug)."""
    r, _ = _run_add(tmp_path, "cmd_subissue_add 42 100")
    hint = next(
        (ln.strip() for ln in r.stderr.splitlines() if "--allow-closed-parent" in ln), ""
    )
    assert hint == "zh subissue add 42 100 --allow-closed-parent", (
        f"override hint must be runnable as-is; got {hint!r}"
    )


def test_subissue_add_override_attaches_and_still_warns(tmp_path) -> None:
    r, fired = _run_add(tmp_path, "cmd_subissue_add 42 100 --allow-closed-parent")
    assert r.returncode == 0, r.stderr
    assert fired, "the override must actually attach"
    assert "roll up to nothing" in r.stderr, "the override must still warn"
    assert "__ZH_BLOCKED__" not in r.stderr, "an override is not a refusal"


def test_subissue_add_override_flag_position_is_irrelevant(tmp_path) -> None:
    """Every planning noun forwards its own "$@", so the flag can arrive last.

    `zh epic add 42 100 --allow-closed-parent` must behave identically to the
    flag-first form; a positional parser would treat the trailing flag as a
    child issue number and die on the numeric guard.
    """
    r, fired = _run_add(
        tmp_path, 'cmd_hierarchy_dispatch "Epic" "epic" add 42 100 --allow-closed-parent'
    )
    assert r.returncode == 0, r.stderr
    assert fired
    assert "Invalid issue number" not in r.stderr


def test_noun_add_refuses_closed_parent(tmp_path) -> None:
    """`zh epic add` routes to cmd_subissue_add, so the guard covers it too."""
    r, fired = _run_add(tmp_path, 'cmd_hierarchy_dispatch "Epic" "epic" add 42 100')
    assert r.returncode != 0
    assert "is CLOSED" in r.stderr
    assert not fired


def test_subissue_add_open_parent_is_unaffected(tmp_path) -> None:
    """The guard must not tax the normal path."""
    r, fired = _run_add(tmp_path, "cmd_subissue_add 42 100", parent_state="OPEN")
    assert r.returncode == 0, r.stderr
    assert fired
    assert "is CLOSED" not in r.stderr


def test_subissue_add_unknown_parent_state_does_not_block(tmp_path) -> None:
    """Fail-soft: a lookup that yields no state must not refuse the attach.

    Only a positively-observed CLOSED blocks. A transient lookup failure (or a
    schema change dropping `state`) degrades to the pre-#92 behavior rather
    than refusing every attach in the workspace.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        zh_resolve_issue_id() { printf 'gid-%s' "$2"; }
        zh_resolve_issue_ids() { printf '%s' '["gid-100"]'; }
        zh_issue_with_parent() { printf 'null'; }
        zh_github_issue_state() { printf '%s' "${ZH_TEST_GH_STATE:-}"; }
        zh_graphql() {
            echo "MUTATION_FIRED" >> "$ZH_TEST_MUTATION_LOG"
            printf '%s' '{"data":{"addSubIssues":{"successCount":1,"failedIssues":[],"githubErrors":[]}}}'
        }
    """
    r, fired = _run_add(tmp_path, "cmd_subissue_add 42 100", stubs=stubs)
    assert r.returncode == 0, r.stderr
    assert fired, "an unknown parent state must not block"


# ==========================================================================
# Bash: zh create --parent
# ==========================================================================

def _create_stubs(parent_state: str = "CLOSED") -> str:
    """Stubs for `cmd_create ... --parent 42`.

    CREATE_FIRED marks the createIssue mutation. The claim under test is that a
    refusal happens BEFORE it: a post-create guard would leave a real issue
    behind on every refusal, which is worse than the defect.
    """
    return r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        get_workspace_id() { printf 'ws-gid'; }
        zh_fetch_issue_types() { printf '[]'; }
        zh_issue_type_id_from() { printf 'tid-task'; }
        zh_issue_type_names_from() { printf 'Task'; }
        zh_resolve_issue_id() { printf 'gid-%s' "$2"; }
        zh_github_issue_state() { printf '%s' "${ZH_TEST_GH_STATE:-}"; }
        zh_graphql() {
            local q="$1" v="$2"
            if [[ "$q" == *createIssue* ]]; then
                echo "CREATE_FIRED" >&2
                printf '%s' '{"data":{"createIssue":{"issue":{"id":"new-gid","number":4242,"htmlUrl":"https://example/4242","title":"T","repository":{"ownerName":"acme","name":"widgets"},"issueType":{"name":"Task"}}}}}'
                return 0
            fi
            if [[ "$q" == *addSubIssues* ]]; then
                printf '%s' '{"data":{"addSubIssues":{"successCount":1,"failedIssues":[]}}}'
                return 0
            fi
            local n
            n=$(printf '%s' "$v" | jq -r '.issueNumber // empty')
            case "$n" in
                42) printf '%s' '{"data":{"issueByInfo":{"id":"gid-42","number":42,"title":"Retired container","state":"__PARENT_STATE__","parentIssue":null}}}' ;;
                *)  printf '%s' '{"data":{"issueByInfo":null}}' ;;
            esac
        }
    """.replace("__PARENT_STATE__", parent_state)


def test_create_refuses_closed_parent_before_creating_anything() -> None:
    """A refusal must leave nothing behind, so it belongs in the pre-flight."""
    r = run_zh_with_stubs(
        _create_stubs("CLOSED"),
        'cmd_create "$@"',
        args=["New work", "-t", "Task", "-b", "Body", "--parent", "42"],
    )
    assert r.returncode != 0, f"expected refusal; got rc=0\n{r.stdout}"
    assert "is CLOSED" in r.stderr
    assert "CREATE_FIRED" not in r.stderr, (
        "the issue was created before the guard ran. A refusal must not leave "
        "a half-filed issue behind"
    )


def test_create_override_creates_and_wires() -> None:
    r = run_zh_with_stubs(
        _create_stubs("CLOSED"),
        'cmd_create "$@"',
        args=["New work", "-t", "Task", "-b", "Body", "--parent", "42",
              "--allow-closed-parent"],
    )
    assert r.returncode == 0, r.stderr
    assert "CREATE_FIRED" in r.stderr
    assert "roll up to nothing" in r.stderr, "the override must still warn"


def test_create_open_parent_is_unaffected() -> None:
    r = run_zh_with_stubs(
        _create_stubs("OPEN"),
        'cmd_create "$@"',
        args=["New work", "-t", "Task", "-b", "Body", "--parent", "42"],
    )
    assert r.returncode == 0, r.stderr
    assert "CREATE_FIRED" in r.stderr
    assert "is CLOSED" not in r.stderr


def test_create_rejects_value_form_of_the_boolean_flag() -> None:
    """`--allow-closed-parent=1` must error, not become the title.

    cmd_create's normalizer splits `--flag=value`; a boolean flag that is not
    registered as one would have its "value" fall through to the positional
    arm and silently replace the user's title.
    """
    r = run_zh_with_stubs(
        _create_stubs("OPEN"),
        'cmd_create "$@"',
        args=["New work", "-t", "Task", "-b", "Body", "--allow-closed-parent=1"],
    )
    assert r.returncode != 0
    assert "boolean flag" in r.stderr, f"got {r.stderr!r}"


# ==========================================================================
# Bash: read paths
# ==========================================================================

def _issue_show_stubs(parent_state: str) -> str:
    return r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        get_workspace_id() { printf 'ws-gid'; }
        build_zenhub_url() { printf 'https://app.zenhub.test/997'; }
        zh_graphql() {
            printf '%s' '{"data":{"issueByInfo":{
                "id":"gid-997","number":997,"title":"Live work","body":"b","state":"OPEN",
                "htmlUrl":"https://example/997","estimate":null,
                "issueType":{"__typename":"GithubIssueType","name":"Task","level":4,"disposition":"BOARD"},
                "assignees":{"nodes":[]},"labels":{"nodes":[]},
                "pipelineIssue":{"pipeline":{"name":"Sprint Backlog"},"priority":null},
                "blockingIssues":{"nodes":[]},"blockedIssues":{"nodes":[]},
                "parentIssue":{"number":593,"title":"Retired container","state":"__PARENT_STATE__"},
                "githubChildIssues":{"totalCount":0},
                "createdAt":"2026-01-01","updatedAt":"2026-01-02"}}}'
        }
    """.replace("__PARENT_STATE__", parent_state)


def test_issue_marks_a_closed_parent() -> None:
    """THE other half of #92: `Parent: #593` said nothing about #593 being dead.

    This line is what an agent reads before deciding an issue is healthy.
    """
    r = run_zh_with_stubs(_issue_show_stubs("CLOSED"), "cmd_issue 997")
    assert r.returncode == 0, r.stderr
    assert "Parent:    #593 (CLOSED)" in r.stdout, (
        f"a closed parent must be marked on read; got {r.stdout!r}"
    )
    assert "zh reparent" in r.stdout, "point at the fix, not just the symptom"


def test_issue_does_not_cry_wolf_on_an_open_parent() -> None:
    r = run_zh_with_stubs(_issue_show_stubs("OPEN"), "cmd_issue 997")
    assert r.returncode == 0, r.stderr
    assert "Parent:    #593 Retired container" in r.stdout
    assert "CLOSED" not in r.stdout
    assert "zh reparent" not in r.stdout


def _subissue_list_stubs(parent_state: str) -> str:
    return r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        get_workspace_id() { printf 'ws-gid'; }
        zh_graphql() {
            printf '%s' '{"data":{"issueByInfo":{
                "number":593,"title":"Retired container","state":"__PARENT_STATE__",
                "githubChildIssues":{"totalCount":1,
                    "pageInfo":{"hasNextPage":false,"endCursor":null},
                    "nodes":[{"number":997,"title":"Live work","state":"OPEN",
                              "assignees":{"nodes":[]},
                              "pipelineIssue":{"pipeline":{"name":"Sprint Backlog"}},
                              "repository":{"ownerName":"acme","name":"widgets"}}]}}}}'
        }
    """.replace("__PARENT_STATE__", parent_state)


def test_subissue_list_marks_a_closed_parent() -> None:
    """A closed container listing live children must say so in the header."""
    r = run_zh_with_stubs(_subissue_list_stubs("CLOSED"), "cmd_subissue_list 593")
    assert r.returncode == 0, r.stderr
    assert "Sub-issues of #593 (CLOSED)" in r.stdout, f"got {r.stdout!r}"
    assert "rolls up to nothing" in r.stdout


def test_subissue_list_open_parent_header_is_unchanged() -> None:
    r = run_zh_with_stubs(_subissue_list_stubs("OPEN"), "cmd_subissue_list 593")
    assert r.returncode == 0, r.stderr
    assert "Sub-issues of #593 Retired container" in r.stdout
    assert "CLOSED" not in r.stdout


# ==========================================================================
# Python: zh_graphql_ops.add_sub_issues (the MCP GraphQL-direct path)
# ==========================================================================

def test_py_add_sub_issues_refuses_closed_parent() -> None:
    """The Python path had the parent's `state` in hand and discarded it.

    Only ONE response is supplied. `patch_ctx_query` raises StopIteration on an
    over-call, so this also proves the guard bails before resolving any child
    and before the mutation, rather than merely relabelling the result.
    """
    ctx = make_ctx()
    with patch_ctx_query(ctx, [issue_by_info_response(42, state="CLOSED")]):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
    assert out["ok"] is False
    assert out["blocked_closed_parent"] is True
    assert out["parent_state"] == "CLOSED"
    assert out["outcome"] == "fail"
    assert out["succeeded"] == []
    # Conservation invariant: nothing was attempted, so every input is
    # unaccounted (the same contract the parent-not-found branch holds).
    assert out["unaccounted"] == [100, 101]
    assert (
        len(out["succeeded"]) + len(out["failed"]) + len(out["unaccounted"]) == 2
    )
    assert "CLOSED" in (out["error"] or "")


def test_py_add_sub_issues_override_attaches_with_a_warning() -> None:
    ctx = make_ctx()
    responses = [
        issue_by_info_response(42, state="CLOSED"),
        issue_by_info_response(100),
        add_sub_issues_response(success_count=1),
    ]
    with patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(
            ctx, 42, [100], allow_closed_parent=True
        )
    assert out["ok"] is True
    assert out["blocked_closed_parent"] is False
    assert out["succeeded"] == [100]
    assert "CLOSED" in (out["closed_parent_warning"] or ""), (
        "an override must still record why the result is suspect"
    )


def test_py_add_sub_issues_open_parent_reports_state_without_blocking() -> None:
    ctx = make_ctx()
    responses = [
        issue_by_info_response(42),
        issue_by_info_response(100),
        add_sub_issues_response(success_count=1),
    ]
    with patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    assert out["ok"] is True
    assert out["parent_state"] == "OPEN"
    assert out["blocked_closed_parent"] is False
    assert out["closed_parent_warning"] is None


def test_py_add_sub_issues_missing_state_does_not_block() -> None:
    """Fail-soft, matching bash: only a positive CLOSED refuses."""
    ctx = make_ctx()
    parent = issue_by_info_response(42)
    del parent["data"]["issueByInfo"]["state"]
    responses = [
        parent,
        issue_by_info_response(100),
        add_sub_issues_response(success_count=1),
    ]
    with patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    assert out["ok"] is True
    assert out["blocked_closed_parent"] is False


# ==========================================================================
# Python: MCP wrapper surfaces
# ==========================================================================

def test_mcp_subissue_add_children_surfaces_the_refusal(monkeypatch) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(mcp_server, "_resolve_ctx", lambda _p: (ctx, None))
    with patch_ctx_query(ctx, [issue_by_info_response(42, state="CLOSED")]):
        out = mcp_server.subissue_add_children(42, [100])
    assert out["ok"] is False
    assert out["blocked_closed_parent"] is True
    assert out["parent_state"] == "CLOSED"
    assert out["partial_applied"] is False, "nothing was attempted"
    assert "CLOSED" in out["stderr"]


def test_mcp_subissue_add_children_keeps_the_key_on_every_path(monkeypatch) -> None:
    """Uniform-key parity: a caller must be able to branch unconditionally."""
    out = mcp_server.subissue_add_children(42, [])
    assert out["blocked_closed_parent"] is False
    assert "parent_state" in out and "closed_parent_warning" in out


def test_blocked_sentinel_is_line_anchored() -> None:
    """A crafted issue title echoed into a warn line must not fake a refusal.

    `warn` lines above the sentinel carry user-controllable text (issue titles,
    raw GraphQL error envelopes), so an unanchored substring search would let a
    caller forge `blocked_closed_parent` on an operation that really ran.
    """
    forged = 'Warning: githubErrors: ["__ZH_BLOCKED__:closed_parent"]\n'
    assert mcp_server._blocked_closed_parent(forged) is False
    genuine = "Warning: something\n__ZH_BLOCKED__:closed_parent\nError: refused\n"
    assert mcp_server._blocked_closed_parent(genuine) is True
    # CR-only line endings still count (mirrors _parse_outcome_sentinel).
    assert mcp_server._blocked_closed_parent(
        "Warning: x\r__ZH_BLOCKED__:closed_parent\r"
    ) is True
    assert mcp_server._blocked_closed_parent("") is False


def test_mcp_planning_add_children_reports_the_refusal(monkeypatch) -> None:
    def fake_run_zh(args, **kwargs):  # noqa: ARG001
        return {"ok": False, "exit_code": 1, "stdout_plain": "",
                "stderr_plain": "__ZH_BLOCKED__:closed_parent\n"
                                "Error: Parent #42 is CLOSED."}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    out = mcp_server.epic_add_children(42, [100])
    assert out["ok"] is False
    assert out["blocked_closed_parent"] is True
    assert out["added"] == [], "a refusal attaches nothing"
    assert out["added_requested"] == [100]


def test_mcp_planning_add_children_forwards_the_override(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run_zh(args, **kwargs):  # noqa: ARG001
        seen.append(args)
        return {"ok": True, "exit_code": 0, "stdout_plain": "",
                "stderr_plain": "__ZH_OUTCOME__:ok"}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    mcp_server.epic_add_children(42, [100], allow_closed_parent=True)
    assert "--allow-closed-parent" in seen[0], f"flag not forwarded: {seen[0]}"

    seen.clear()
    mcp_server.epic_add_children(42, [100])
    assert "--allow-closed-parent" not in seen[0], "flag leaked into the default call"


def test_mcp_move_children_refusal_is_not_a_partial(monkeypatch) -> None:
    """The refusal precedes the detach, so no child can have moved.

    `partial_applied` greps stderr for "Attached"; a refusal message that
    happened to contain that word would otherwise report a half-done move that
    never started.
    """
    def fake_run_zh(args, **kwargs):  # noqa: ARG001
        return {"ok": False, "exit_code": 1, "stdout_plain": "",
                "stderr_plain": "__ZH_BLOCKED__:closed_parent\n"
                                "Error: Parent #586 is CLOSED. Attached nothing."}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    out = mcp_server.move_children(to=586, issue_numbers=[60])
    assert out["blocked_closed_parent"] is True
    assert out["partial_applied"] is False


def test_mcp_create_issue_surfaces_the_refusal(monkeypatch) -> None:
    def fake_run_zh(args, **kwargs):  # noqa: ARG001
        return {"ok": False, "exit_code": 1, "stdout_plain": "",
                "stderr_plain": "__ZH_BLOCKED__:closed_parent\n"
                                "Error: Parent #593 is CLOSED."}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    out = mcp_server.create_issue(
        "Title", "Body", parent=593, skip_duplicate_check=True
    )
    assert out["ok"] is False
    assert out["blocked_closed_parent"] is True
    assert out["number"] is None, "a refusal must leave no issue behind"


def test_mcp_create_issue_forwards_the_override(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run_zh(args, **kwargs):  # noqa: ARG001
        seen.append(args)
        return {"ok": False, "exit_code": 1, "stdout_plain": "", "stderr_plain": ""}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    mcp_server.create_issue("T", "B", parent=593, skip_duplicate_check=True,
                            allow_closed_parent=True)
    assert "--allow-closed-parent" in seen[0]

    seen.clear()
    # No parent means no parent guard to override, so the flag must not appear.
    mcp_server.create_issue("T", "B", skip_duplicate_check=True,
                            allow_closed_parent=True)
    assert "--allow-closed-parent" not in seen[0]


# ==========================================================================
# GitHub is authoritative when ZenHub's mirror has lapsed
# ==========================================================================
#
# The first cut of this guard trusted ZenHub's `Issue.state` alone and passed
# its whole stub suite while doing nothing in production. Verified live
# 2026-08-17: in a workspace whose ZenHub<->GitHub authorization had lapsed,
# four GitHub-closed issues all reported `state: OPEN` with a null `closedAt`,
# and the guard never fired. A stale mirror is precisely when a closed parent
# is most likely to be attached to by mistake, so these cases pin the fallback.


def test_guard_fires_on_github_state_when_zenhub_says_open(tmp_path) -> None:
    """THE lapsed-mirror case: ZenHub says OPEN, GitHub says CLOSED."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _subissue_add_stubs("OPEN"),          # ZenHub's (stale) answer
        "cmd_subissue_add 42 100",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log),
                   "ZH_TEST_GH_STATE": "CLOSED"},  # GitHub's real answer
    )
    assert r.returncode != 0, (
        "a parent closed on GitHub must be refused even when ZenHub's mirror "
        f"still calls it OPEN; got rc=0\n{r.stdout}"
    )
    assert "MUTATION_FIRED" not in log.read_text()


def test_lapsed_mirror_is_diagnosed_not_just_refused(tmp_path) -> None:
    """The operator is about to wonder why the board disagrees. Say why."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _subissue_add_stubs("OPEN"),
        "cmd_subissue_add 42 100",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log),
                   "ZH_TEST_GH_STATE": "CLOSED"},
    )
    assert "sync for this workspace has lapsed" in r.stderr, (
        f"a mirror disagreement must be named, not silently absorbed; got {r.stderr!r}"
    )


def test_merged_pr_number_is_treated_as_closed(tmp_path) -> None:
    """Issues and PRs share a number namespace; a merged PR is not a container."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _subissue_add_stubs("OPEN"),
        "cmd_subissue_add 42 100",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log),
                   "ZH_TEST_GH_STATE": "MERGED"},
    )
    assert r.returncode != 0, "a merged PR passed as a parent must be refused"
    assert "MUTATION_FIRED" not in log.read_text()


def test_github_open_does_not_override_zenhub_closed(tmp_path) -> None:
    """The union runs both ways: CLOSED from EITHER source refuses.

    Resolving a disagreement toward refusal is the recoverable direction: a
    wrong refusal costs one flag, a wrong allow costs a silent orphan.
    """
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _subissue_add_stubs("CLOSED"),
        "cmd_subissue_add 42 100",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log),
                   "ZH_TEST_GH_STATE": "OPEN"},
    )
    assert r.returncode != 0
    assert "MUTATION_FIRED" not in log.read_text()
    assert "has lapsed" not in r.stderr, (
        "ZenHub-closed / GitHub-open is not the lapsed-mirror shape and must "
        "not claim it is"
    )


def test_both_open_proceeds(tmp_path) -> None:
    """Control: agreement on OPEN must not be taxed by the second lookup."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _subissue_add_stubs("OPEN"),
        "cmd_subissue_add 42 100",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log),
                   "ZH_TEST_GH_STATE": "OPEN"},
    )
    assert r.returncode == 0, r.stderr
    assert "MUTATION_FIRED" in log.read_text()


def test_py_guard_uses_github_when_zenhub_mirror_is_stale(monkeypatch) -> None:
    """Python path: same lapsed-mirror fallback as bash.

    Only ONE response is supplied, so `patch_ctx_query` would raise
    StopIteration if the guard resolved a child or reached the mutation.
    """
    monkeypatch.setattr(
        zh_graphql_ops, "get_gh_issue_state", lambda *a, **k: "CLOSED"
    )
    ctx = make_ctx()
    with patch_ctx_query(ctx, [issue_by_info_response(42, state="OPEN")]):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    assert out["blocked_closed_parent"] is True
    assert out["parent_state"] == "CLOSED"
    assert out["unaccounted"] == [100]


def test_py_guard_treats_merged_as_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        zh_graphql_ops, "get_gh_issue_state", lambda *a, **k: "MERGED"
    )
    ctx = make_ctx()
    with patch_ctx_query(ctx, [issue_by_info_response(42, state="OPEN")]):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    assert out["blocked_closed_parent"] is True


def test_py_guard_fails_soft_when_github_is_unreachable(monkeypatch) -> None:
    """gh missing / unauthenticated / rate-limited must not block every attach."""
    monkeypatch.setattr(
        zh_graphql_ops, "get_gh_issue_state", lambda *a, **k: None
    )
    ctx = make_ctx()
    responses = [
        issue_by_info_response(42, state="OPEN"),
        issue_by_info_response(100),
        add_sub_issues_response(success_count=1),
    ]
    with patch_ctx_query(ctx, responses):
        out = zh_graphql_ops.add_sub_issues(ctx, 42, [100])
    assert out["ok"] is True
    assert out["blocked_closed_parent"] is False


def test_gh_issue_state_returns_none_without_a_token(monkeypatch) -> None:
    """The fail-soft contract holds at the helper boundary, not just above it."""
    import zh_api
    monkeypatch.setattr(
        zh_api.subprocess, "check_output",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no gh")),
    )
    assert zh_api.get_gh_issue_state("acme/widgets", 42) is None


def test_gh_issue_state_normalizes_case(monkeypatch) -> None:
    """GitHub REST returns lowercase; ZenHub uses uppercase. Compare cleanly."""
    import io
    import zh_api

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(zh_api.subprocess, "check_output", lambda *a, **k: "tok\n")
    monkeypatch.setattr(
        zh_api.urllib.request, "urlopen",
        lambda *a, **k: _Resp(b'{"state": "closed"}'),
    )
    assert zh_api.get_gh_issue_state("acme/widgets", 42) == "CLOSED"


def test_guard_still_runs_when_the_zenhub_lookup_fails(tmp_path) -> None:
    """A failed ZenHub lookup must not take the whole guard down with it.

    The guard is called unconditionally rather than from inside the
    `parent_info != null` branch that resolves the parent's ZenHub id. Nesting
    it there reads as harmless (no ZenHub state to check) but disables the
    GitHub check too, in exactly the situation where it matters most: a
    workspace whose mirror is failing is the one most likely to be hiding a
    closed parent.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        get_repo_id() { printf 'repo-gid'; }
        zh_issue_with_parent() { printf 'null'; }
        zh_resolve_issue_id() { printf 'gid-%s' "$2"; }
        zh_resolve_issue_ids() { printf '%s' '["gid-100"]'; }
        zh_github_issue_state() { printf '%s' "${ZH_TEST_GH_STATE:-}"; }
        zh_graphql() {
            echo "MUTATION_FIRED" >> "$ZH_TEST_MUTATION_LOG"
            printf '%s' '{"data":{"addSubIssues":{"successCount":1,"failedIssues":[],"githubErrors":[]}}}'
        }
    """
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        stubs, "cmd_subissue_add 42 100",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log),
                   "ZH_TEST_GH_STATE": "CLOSED"},
    )
    assert r.returncode != 0, (
        "GitHub said CLOSED; a dead ZenHub lookup must not smuggle the attach "
        f"through\n{r.stdout}"
    )
    assert "MUTATION_FIRED" not in log.read_text()


# ==========================================================================
# The close side: warn about the children this close is about to orphan
# ==========================================================================
#
# `cmd_close` has warned since v1.11.0, but the warning had NO test coverage
# and had never been observed firing. It is also the highest-value path in this
# whole area: closing a parent that still has open children is what actually
# produces orphans, whereas attaching to an already-closed parent is the rarer
# follow-on mistake. An untested warning on that path was the weakest link.
#
# The `gh` stub emulates `gh api --jq <expr>` by piping a fixed payload through
# real jq, rather than returning pre-filtered numbers. That is deliberate: the
# `select(.state=="open")` filter IS zh's code (it lives in the --jq argument),
# so a stub that returned an already-filtered list would leave the filter
# itself unexercised and a mutation of it undetected.

_CLOSE_PAYLOAD_MIXED = (
    '[{"number":997,"state":"open"},'
    ' {"number":998,"state":"closed"},'
    ' {"number":1013,"state":"open"}]'
)
_CLOSE_PAYLOAD_ALL_CLOSED = (
    '[{"number":998,"state":"closed"},{"number":999,"state":"closed"}]'
)


def _close_stubs(sub_issues_json: str) -> str:
    return r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        SUB_ISSUES_JSON='__PAYLOAD__'
        gh() {
            if [[ "$1" == "api" ]]; then
                # Emulate `gh api --paginate --jq <expr>`: run zh's OWN jq
                # expression against the payload so the filter is under test.
                local expr="" prev="" a
                for a in "$@"; do
                    [[ "$prev" == "--jq" ]] && expr="$a"
                    prev="$a"
                done
                printf '%s' "$SUB_ISSUES_JSON" | jq -r "$expr"
                return 0
            fi
            case "$2" in
                view)  printf 'Retired container' ;;
                close) echo "CLOSE_FIRED" >> "$ZH_TEST_MUTATION_LOG"; return 0 ;;
            esac
        }
    """.replace("__PAYLOAD__", sub_issues_json)


def test_close_warns_about_the_children_it_will_orphan(tmp_path) -> None:
    """THE path that produces orphans. Never observed firing before this test."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _close_stubs(_CLOSE_PAYLOAD_MIXED), "cmd_close 593",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log)},
    )
    assert r.returncode == 0, r.stderr
    assert "still has open sub-issue(s): #997, #1013" in r.stderr, (
        f"the close must name the children it is about to orphan; got {r.stderr!r}"
    )
    assert "roll up to nothing" in r.stdout
    # A warning, not a block: the close must still happen.
    assert "CLOSE_FIRED" in log.read_text(), (
        "closing is not blocked by open children, only announced"
    )


def test_close_warning_lists_only_open_children(tmp_path) -> None:
    """#998 is already closed and must not appear: it is not being orphaned."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _close_stubs(_CLOSE_PAYLOAD_MIXED), "cmd_close 593",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log)},
    )
    assert "#998" not in r.stderr and "#998" not in r.stdout, (
        f"an already-closed child is not orphaned by this close; got {r.stderr!r}"
    )


def test_close_reparent_hint_is_copy_pasteable(tmp_path) -> None:
    """Space-separated, hash-free numbers, or `zh reparent` rejects them."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _close_stubs(_CLOSE_PAYLOAD_MIXED), "cmd_close 593",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log)},
    )
    hint = next(
        (ln for ln in r.stdout.splitlines() if "zh reparent" in ln), ""
    )
    assert hint.strip().endswith("zh reparent <new_parent#> 997 1013"), (
        f"hint must carry bare space-separated numbers; got {hint!r}"
    )


def test_close_is_silent_when_no_child_would_be_orphaned(tmp_path) -> None:
    """Don't cry wolf: every child already closed means nothing is orphaned."""
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        _close_stubs(_CLOSE_PAYLOAD_ALL_CLOSED), "cmd_close 593",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log)},
    )
    assert r.returncode == 0, r.stderr
    assert "still has open sub-issue" not in r.stderr, (
        f"no open children means no warning; got {r.stderr!r}"
    )
    assert "CLOSE_FIRED" in log.read_text()


def test_close_survives_a_failed_sub_issue_lookup(tmp_path) -> None:
    """Fail-soft: a lookup failure must never stop a close.

    GitHub's sub_issues endpoint is comparatively new; a 404 or a gh without
    the subcommand must degrade to "close without the warning", not to a
    failed close.
    """
    stubs = r"""
        load_config() { :; }
        get_repo_info() { printf 'acme/widgets'; }
        gh() {
            if [[ "$1" == "api" ]]; then
                echo "gh: Not Found (HTTP 404)" >&2
                return 1
            fi
            case "$2" in
                view)  printf 'Retired container' ;;
                close) echo "CLOSE_FIRED" >> "$ZH_TEST_MUTATION_LOG"; return 0 ;;
            esac
        }
    """
    log = tmp_path / "mutations.log"
    log.write_text("")
    r = run_zh_with_stubs(
        stubs, "cmd_close 593",
        extra_env={"ZH_TEST_MUTATION_LOG": str(log)},
    )
    assert r.returncode == 0, f"a failed lookup must not fail the close: {r.stderr!r}"
    assert "CLOSE_FIRED" in log.read_text()
