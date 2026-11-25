"""`zh reorder` places a card relative to an open card and confirms it by reading
the pipeline back.

The stand-in ZenHub keeps one pipeline as an ordered list of cards, closed
ones included, the way a real pipeline keeps closed issues filed in it. Like
the live API, `searchIssuesByPipeline` leaves the closed cards out (unless a
test asks for them) while moves still apply to the full list. A
`moveIssueRelativeTo` call is applied to that list unless the test asks for
a silent no-op, which is how the real API failed for `moveIssue(position: 0)`.
"""

from __future__ import annotations

import json
import shlex

import pytest

from tests._bash_runner import run_zh_with_stubs

# Board order, top to bottom. Closed cards sit above and between open ones.
CARDS = [
    {"pi": "pi-c1", "n": 901, "state": "CLOSED"},
    {"pi": "pi-c2", "n": 902, "state": "CLOSED"},
    {"pi": "pi-1", "n": 1, "state": "OPEN"},
    {"pi": "pi-2", "n": 2, "state": "OPEN"},
    {"pi": "pi-c3", "n": 903, "state": "CLOSED"},
    {"pi": "pi-3", "n": 3, "state": "OPEN"},
    {"pi": "pi-4", "n": 4, "state": "OPEN"},
    {"pi": "pi-c4", "n": 904, "state": "CLOSED"},
]

APPLY_MOVE = r'''
    ($v.input.issueId | ltrimstr("issue-") | tonumber) as $n |
    ([.[] | select(.n == $n)][0]) as $card |
    [.[] | select(.n != $n)] as $rest |
    (if $v.input.beforePipelineIssueId then
        ($rest | map(.pi) | index($v.input.beforePipelineIssueId))
     else
        ($rest | map(.pi) | index($v.input.afterPipelineIssueId)) + 1
     end) as $at |
    $rest[:$at] + [$card] + $rest[$at:]
'''


def _stubs(cards=None, *, noop=False, other_pipeline=(), reread_partial=False,
           mutation_fails=False, search_shows_closed=False, total_extra=0,
           null_pi_for=0, insert_on_move=False):
    initial = shlex.quote(json.dumps(CARDS if cards is None else cards))
    apply_move = shlex.quote(APPLY_MOVE)
    elsewhere = " ".join(str(n) for n in other_pipeline)
    return f'''
    load_config() {{ :; }}
    get_repo_info() {{ printf acme/widgets; }}
    get_repo_id() {{ printf repo-gid; }}
    get_workspace_id() {{ printf ws-gid; }}
    state="$HOME/cards.json"
    printf '%s' {initial} > "$state"
    zh_graphql() {{
        local q="$1" v="$2"
        if [[ "$q" == *moveIssueRelativeTo* ]]; then
            echo "MUTATION=$(printf '%s' "$v" | jq -c .)" >&2
            touch "$state.moved"
            if [[ {str(mutation_fails).lower()} == true ]]; then
                error "refused by ZenHub"
            fi
            if [[ {str(noop).lower()} != true ]]; then
                jq --argjson v "$v" {apply_move} "$state" > "$state.next"
                mv "$state.next" "$state"
            fi
            if [[ {str(insert_on_move).lower()} == true ]]; then
                jq '[{{pi: "pi-new", n: 77, state: "OPEN"}}] + .' "$state" > "$state.next"
                mv "$state.next" "$state"
            fi
            printf '%s' '{{"data":{{"moveIssueRelativeTo":{{"clientMutationId":null}}}}}}'
        elif [[ "$q" == *moveIssue* ]]; then
            echo "OLD_MOVE_ISSUE_CALLED" >&2
            printf '%s' '{{"data":{{"moveIssue":{{"issue":{{"id":"x"}}}}}}}}'
        elif [[ "$q" == *issueByInfo* ]]; then
            local n pid
            n=$(printf '%s' "$v" | jq -r .issueNumber)
            pid=p-todo
            for e in {elsewhere}; do [[ "$e" == "$n" ]] && pid=p-other; done
            # The card belongs to workspace ws-gid only, so a lookup that does
            # not ask for this workspace's card finds none.
            if [[ "$(printf '%s' "$v" | jq -r .workspaceId)" != ws-gid ]]; then
                printf '%s' '{{"data":{{"issueByInfo":{{"id":"x","number":1,"title":"t","pipelineIssue":null}}}}}}'
                return
            fi
            jq --argjson n "$n" --arg pid "$pid" '([.[] | select(.n == $n)][0]) as $c |
                {{data: {{issueByInfo: (if $c == null then null else
                    {{id: ("issue-" + ($n|tostring)), number: $n, title: ("Issue " + ($n|tostring)),
                     pipelineIssue: {{id: $c.pi, pipeline: {{id: $pid, name: "TO DO"}}}}}} end)}}}}' "$state"
        elif [[ "$q" == *searchIssuesByPipeline* ]]; then
            if [[ {str(reread_partial).lower()} == true && -f "$state.moved" ]]; then
                printf '%s' '{{"data":{{"searchIssuesByPipeline":null}}}}'; return
            fi
            jq --argjson all {str(search_shows_closed).lower()} \
               --argjson extra {total_extra} --argjson nullpi {null_pi_for} \
               'map(select($all or .state != "CLOSED")) |
               {{data: {{searchIssuesByPipeline: {{totalCount: (length + $extra),
                pageInfo: {{hasNextPage: false, endCursor: null}},
                nodes: map({{number: .n, title: "t", state: .state, zenhubUrl: "u",
                    estimate: null, assignees: {{nodes: []}},
                    repository: {{ownerName: "acme", name: "widgets"}},
                    pipelineIssue: (if .n == $nullpi then null else {{id: .pi}} end)}})}}}}}}' "$state"
        else
            printf '%s' '{{"data":{{}}}}'
        fi
    }}
    '''


def _run(args, **kw):
    return run_zh_with_stubs(_stubs(**kw), 'main reorder "$@"', args=args)


def _moves(r):
    return [json.loads(line.removeprefix("MUTATION="))["input"]
            for line in r.stderr.splitlines() if line.startswith("MUTATION=")]


def _json(r):
    return json.loads(r.stdout)


def test_top_goes_above_the_first_open_card_not_position_zero():
    r = _run(["4", "top", "--json"])
    assert r.returncode == 0, r.stderr
    assert _moves(r) == [{"issueId": "issue-4", "beforePipelineIssueId": "pi-1"}]
    assert "OLD_MOVE_ISSUE_CALLED" not in r.stderr
    out = _json(r)
    assert out["verified"] is True and out["open_position"] == 0 and out["open_count"] == 4


def test_closed_cards_in_the_search_result_are_skipped():
    r = _run(["4", "top", "--json"], search_shows_closed=True)
    assert r.returncode == 0, r.stderr
    assert _moves(r) == [{"issueId": "issue-4", "beforePipelineIssueId": "pi-1"}]
    assert _json(r)["open_position"] == 0 and _json(r)["open_count"] == 4
    r = _run(["1", "2", "--json"], search_shows_closed=True)
    assert _moves(r) == [{"issueId": "issue-1", "beforePipelineIssueId": "pi-4"}]
    assert _json(r)["open_position"] == 2


def test_bottom_goes_below_the_last_open_card():
    r = _run(["1", "bottom", "--json"])
    assert r.returncode == 0, r.stderr
    assert _moves(r) == [{"issueId": "issue-1", "afterPipelineIssueId": "pi-4"}]
    assert _json(r)["open_position"] == 3


@pytest.mark.parametrize("pos,expected,final", [
    ("0", {"beforePipelineIssueId": "pi-2"}, 0),
    ("1", {"beforePipelineIssueId": "pi-3"}, 1),
    ("2", {"beforePipelineIssueId": "pi-4"}, 2),
    ("3", {"afterPipelineIssueId": "pi-4"}, 3),
    ("99", {"afterPipelineIssueId": "pi-4"}, 3),
])
def test_numeric_position_counts_open_cards_only(pos, expected, final):
    r = _run(["1", pos, "--json"])
    assert r.returncode == 0, r.stderr
    assert _moves(r) == [{"issueId": "issue-1", **expected}]
    assert _json(r)["open_position"] == final


def test_before_and_after_another_issue():
    r = _run(["4", "before", "2", "--json"])
    assert r.returncode == 0, r.stderr
    assert _moves(r) == [{"issueId": "issue-4", "beforePipelineIssueId": "pi-2"}]
    assert _json(r)["open_position"] == 1
    r = _run(["#1", "after", "#3", "--json"])
    assert r.returncode == 0, r.stderr
    assert _moves(r) == [{"issueId": "issue-1", "afterPipelineIssueId": "pi-3"}]
    assert _json(r)["open_position"] == 2


def test_a_move_that_silently_does_nothing_is_reported_as_a_failure():
    r = _run(["4", "top"], noop=True)
    assert r.returncode == 1
    assert "not where it was asked to go" in r.stderr
    assert "Reordered" not in r.stdout


def test_json_reports_unverified_when_the_move_did_nothing():
    r = _run(["4", "top", "--json"], noop=True)
    assert r.returncode == 1
    assert _json(r)["verified"] is False and _json(r)["open_position"] == 3


def test_an_incomplete_read_back_exits_2_unconfirmed():
    r = _run(["4", "top"], reread_partial=True)
    assert r.returncode == 2
    assert "unconfirmed" in r.stderr
    assert len(_moves(r)) == 1


def test_a_refused_mutation_exits_1():
    """The real zh_graphql calls error itself on a GraphQL error."""
    r = _run(["4", "top", "--json"], mutation_fails=True)
    assert r.returncode == 1
    assert "refused by ZenHub" in r.stderr
    assert r.stdout == ""


def test_anchor_in_another_pipeline_is_refused_before_any_move():
    r = _run(["4", "before", "2"], other_pipeline=(2,))
    assert r.returncode == 1
    assert "is not in 'TO DO'" in r.stderr
    assert _moves(r) == []


def test_the_only_open_card_needs_no_move():
    cards = [{"pi": "pi-c1", "n": 901, "state": "CLOSED"},
             {"pi": "pi-1", "n": 1, "state": "OPEN"}]
    r = _run(["1", "top"], cards=cards)
    assert r.returncode == 0, r.stderr
    assert _moves(r) == []
    assert "only open card" in r.stdout


@pytest.mark.parametrize("args,msg", [
    (["4", "before"], "needs an issue number"),
    (["4", "after", "4"], "itself"),
    (["4", "sideways"], "Invalid position"),
    (["4", "top", "2"], "Usage"),
    (["x", "top"], "Invalid issue number"),
])
def test_bad_arguments_are_refused_before_any_call(args, msg):
    r = _run(args)
    assert r.returncode == 1
    assert msg in r.stderr
    assert _moves(r) == []


def test_help_lists_before_and_after():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "before/after <issue>" in r.stdout


# --- MCP reorder_issue -------------------------------------------------------

def _mcp_through_real_zh(monkeypatch, **stub_kw):
    """Route mcp_server._run_zh through the real cmd_reorder and the stand-in."""
    import mcp_server

    calls = []

    def fake_run_zh(args, *, cwd=None, stdin=None, timeout=None):
        calls.append(list(args))
        r = run_zh_with_stubs(_stubs(**stub_kw), 'main "$@"', args=list(args))
        return {"ok": r.returncode == 0, "exit_code": r.returncode,
                "stdout_plain": r.stdout, "stderr_plain": r.stderr, "stderr": r.stderr}

    monkeypatch.setattr(mcp_server, "_run_zh", fake_run_zh)
    return mcp_server, calls


def test_mcp_reorder_top_is_verified_through_real_zh(monkeypatch):
    mcp_server, calls = _mcp_through_real_zh(monkeypatch)
    out = mcp_server.reorder_issue(4, "top")
    assert calls == [["reorder", "--json", "--", "4", "top"]]
    assert out["ok"] is True and out["verified"] is True
    assert out["open_position"] == 0 and out["open_count"] == 4
    assert out["pipeline"] == "TO DO" and out["partial_applied"] is False


def test_mcp_reorder_after_passes_relative_to(monkeypatch):
    mcp_server, calls = _mcp_through_real_zh(monkeypatch)
    out = mcp_server.reorder_issue(1, "after", relative_to=3)
    assert calls == [["reorder", "--json", "--", "1", "after", "3"]]
    assert out["ok"] is True and out["open_position"] == 2


def test_mcp_reorder_silent_noop_is_not_ok(monkeypatch):
    mcp_server, _ = _mcp_through_real_zh(monkeypatch, noop=True)
    out = mcp_server.reorder_issue(4, "top")
    assert out["ok"] is False and out["verified"] is False
    assert out["open_position"] == 3 and out["unconfirmed"] is False
    assert "not where it was asked to go" in out["stderr"]


def test_mcp_reorder_unconfirmed_read_back(monkeypatch):
    mcp_server, _ = _mcp_through_real_zh(monkeypatch, reread_partial=True)
    out = mcp_server.reorder_issue(4, "top")
    assert out["ok"] is False and out["unconfirmed"] is True
    assert out["partial_applied"] is True


@pytest.mark.parametrize("position,relative_to,msg", [
    ("before", 0, "needs relative_to"),
    ("After", 0, "needs relative_to"),
    ("top", 7, "only used with"),
])
def test_mcp_reorder_argument_errors_send_nothing(monkeypatch, position, relative_to, msg):
    mcp_server, calls = _mcp_through_real_zh(monkeypatch)
    out = mcp_server.reorder_issue(4, position, relative_to=relative_to)
    assert out["ok"] is False and msg in out["stderr"]
    assert calls == []


def test_mcp_reorder_position_is_case_insensitive(monkeypatch):
    mcp_server, calls = _mcp_through_real_zh(monkeypatch)
    out = mcp_server.reorder_issue(4, " Before ", relative_to=2)
    assert calls == [["reorder", "--json", "--", "4", "before", "2"]]
    assert out["ok"] is True and out["open_position"] == 1


@pytest.mark.parametrize("args", [["1", "bottom"], ["1", "after", "3"], ["4", "before", "2"], ["4", "2"]])
def test_every_kind_of_silent_noop_is_caught(args):
    r = _run(args + ["--json"], noop=True)
    assert r.returncode == 1, r.stderr
    out = _json(r)
    assert out["sent"] is True and out["verified"] is False


def test_a_card_moved_by_someone_else_meanwhile_is_not_confirmed():
    """Someone else's card lands on top between the move and the read-back:
    the card is next to its target but no longer at the position asked for."""
    r = _run(["4", "top", "--json"], insert_on_move=True)
    assert r.returncode == 1
    assert _json(r)["open_position"] == 1
    assert "asked for open card 0" in r.stderr


def test_a_pipeline_read_shorter_than_its_total_moves_nothing():
    r = _run(["4", "top"], total_extra=1)
    assert r.returncode == 1
    assert "Could not read every card" in r.stderr
    assert _moves(r) == []


def test_a_card_without_its_card_id_makes_the_read_incomplete():
    r = _run(["4", "top"], null_pi_for=1)
    assert r.returncode == 1
    assert "Could not read every card" in r.stderr
    assert _moves(r) == []


@pytest.mark.parametrize("args,msg", [
    (["901", "top"], "#901 is closed"),
    (["4", "before", "903"], "#903 is closed"),
    (["4", "after", "903"], "#903 is closed"),
])
def test_closed_cards_are_refused_before_any_move(args, msg):
    r = _run(args)
    assert r.returncode == 1
    assert msg in r.stderr
    assert _moves(r) == []


def test_closed_cards_are_refused_when_search_returns_them_too():
    r = _run(["4", "before", "903"], search_shows_closed=True)
    assert r.returncode == 1 and "#903 is closed" in r.stderr and _moves(r) == []


def test_leading_zeros_are_decimal_and_name_the_same_issue():
    r = _run(["4", "after", "04"])
    assert r.returncode == 1 and "itself" in r.stderr and _moves(r) == []
    r = _run(["04", "08", "--json"])
    assert r.returncode == 0, r.stderr
    assert _json(r)["number"] == 4 and _json(r)["open_position"] == 3


@pytest.mark.parametrize("args,msg", [
    (["1", "18446744073709551615"], "Invalid position"),
    (["1234567890", "top"], "Invalid issue number"),
    (["4", "after", "1234567890"], "needs an issue number"),
])
def test_oversized_numbers_are_refused(args, msg):
    r = _run(args)
    assert r.returncode == 1 and msg in r.stderr and _moves(r) == []


def test_only_open_card_reports_nothing_sent():
    cards = [{"pi": "pi-c1", "n": 901, "state": "CLOSED"},
             {"pi": "pi-1", "n": 1, "state": "OPEN"}]
    r = _run(["1", "bottom", "--json"], cards=cards, reread_partial=True)
    assert r.returncode == 0, r.stderr
    assert _json(r) == {"number": 1, "pipeline": "TO DO", "requested": "bottom",
                        "sent": False, "verified": True, "open_position": 0, "open_count": 1}


def test_mcp_moved_to_the_wrong_place_is_partial_applied(monkeypatch):
    mcp_server, _ = _mcp_through_real_zh(monkeypatch, insert_on_move=True)
    out = mcp_server.reorder_issue(4, "top")
    assert out["ok"] is False and out["partial_applied"] is True
    assert out["unconfirmed"] is False and out["open_position"] == 1


@pytest.mark.parametrize("exit_code,stdout,ok,unconfirmed,partial", [
    (0, '{"verified": false, "sent": true}', False, False, True),
    (0, '{"verified": true, "sent": true}', True, False, False),
    (2, '', False, False, False),
    (2, '{"verified": false, "sent": true}', False, True, True),
    (1, 'not json', False, False, False),
])
def test_mcp_result_follows_verified_and_sent(monkeypatch, exit_code, stdout, ok, unconfirmed, partial):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_run_zh", lambda *a, **k: {
        "ok": exit_code == 0, "exit_code": exit_code, "stdout_plain": stdout,
        "stderr_plain": "", "stderr": ""})
    out = mcp_server.reorder_issue(4, "top")
    assert (out["ok"], out["unconfirmed"], out["partial_applied"]) == (ok, unconfirmed, partial)
