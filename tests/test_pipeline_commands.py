"""Pipeline CLI mutations use mocked GraphQL and assert exact input variables."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import shlex
import subprocess

import pytest

from tests._bash_runner import run_zh_with_stubs


ROWS = [
    {"id": "p1", "name": "Backlog", "issues": {"totalCount": 0}},
    {"id": "p2", "name": "Doing", "issues": {"totalCount": 0}},
    {"id": "p3", "name": "Done", "issues": {"totalCount": 0}},
]


def _stubs(rows=None, *, mutation_null=False, list_null=False, reread_fail=False):
    initial = shlex.quote(json.dumps(ROWS if rows is None else rows))
    return f'''
    load_config() {{ :; }}
    get_repo_info() {{ printf acme/widgets; }}
    get_workspace_id() {{ printf ws-gid; }}
    pipeline_state="$HOME/pipelines.json"
    printf '%s' {initial} > "$pipeline_state"
    zh_graphql() {{
        local q="$1" v="$2" op
        if [[ "$q" == *mutation* ]]; then
            echo "MUTATION=$(printf '%s' "$v" | jq -c .)" >&2
            echo "QUERY=$q" >&2
            if [[ {str(mutation_null).lower()} == true ]]; then
                printf '%s' '{{"data":{{"updatePipeline":null}}}}'; return
            fi
            if [[ "$q" == *createPipeline* ]]; then
                op=createPipeline
                jq --argjson v "$v" '.[:$v.input.position] +
                    [{{id:"p-new",name:$v.input.name,issues:{{totalCount:0}}}}] +
                    .[$v.input.position:]' "$pipeline_state" > "$pipeline_state.next"
            elif [[ "$q" == *updatePipeline* ]]; then
                op=updatePipeline
                jq --argjson v "$v" '
                    if $v.input | has("position") then
                        ([.[] | select(.id == $v.input.pipelineId)][0]) as $target |
                        [.[] | select(.id != $v.input.pipelineId)] |
                        .[:$v.input.position] + [$target] + .[$v.input.position:]
                    else map(if .id == $v.input.pipelineId then .name = $v.input.name else . end) end
                    ' "$pipeline_state" > "$pipeline_state.next"
            else
                op=deletePipeline
                jq --argjson v "$v" '[.[] | select(.id != $v.input.pipelineId)]' \
                    "$pipeline_state" > "$pipeline_state.next"
            fi
            mv "$pipeline_state.next" "$pipeline_state"
            touch "$pipeline_state.mutated"
            printf '{{"data":{{"%s":{{"__typename":"Payload"}}}}}}' "$op"
        else
            echo "READ=$q" >&2
            echo "READ_VARS=$(printf '%s' "$v" | jq -c .)" >&2
            if [[ {str(list_null).lower()} == true ]] || \
                [[ {str(reread_fail).lower()} == true && -f "$pipeline_state.mutated" ]]; then
                printf '%s' '{{"data":{{"workspace":null}}}}'; return
            fi
            jq '{{data:{{workspace:{{pipelinesConnection:{{nodes:.}}}}}}}}' "$pipeline_state"
        fi
    }}
    '''


def _run(args, **kwargs):
    return run_zh_with_stubs(_stubs(**kwargs), 'main pipeline "$@"', args=args)


def _mutation(r):
    return [json.loads(line.removeprefix("MUTATION=")) for line in r.stderr.splitlines()
            if line.startswith("MUTATION=")]


@pytest.mark.parametrize("flags,index", [
    ([], 3), (["--position", "0"], 0), (["--position", "1"], 1),
    (["--position", "3"], 3), (["--position", "0001"], 1),
    (["--after", "BACKLOG"], 1), (["--before", "Done"], 2),
])
def test_create_exact_variables_and_resulting_order(flags, index):
    r = _run(["create", "Review", *flags, "-d", "Ready for review"])
    assert r.returncode == 0, r.stderr
    assert _mutation(r) == [{"input": {"workspaceId": "ws-gid", "name": "Review",
                                      "position": index, "description": "Ready for review"}}]
    assert f"* {index}: Review" in r.stdout
    assert r.stderr.count("READ_VARS=") == 2
    assert 'READ_VARS={"workspaceId":"ws-gid"}' in r.stderr
    assert "CreatePipelineInput!" in r.stderr


def test_create_json_and_quiet_keep_stdout_machine_readable():
    r = _run(["create", "Review", "--json"])
    assert r.returncode == 0, r.stderr
    assert _mutation(r)[0]["input"] == {"workspaceId": "ws-gid", "name": "Review", "position": 3}
    doc = json.loads(r.stdout)
    assert doc["id"] == "p-new" and doc["name"] == "Review"
    assert [row["name"] for row in doc["pipelines"]] == ["Backlog", "Doing", "Done", "Review"]
    assert [row["changed"] for row in doc["pipelines"]] == [False, False, False, True]
    r = _run(["create", "Review", "-q"])
    assert r.stdout == "p-new\n"
    assert "* 3: Review" in r.stderr


@pytest.mark.parametrize("name,flags,index", [
    ("Backlog", ["--after", "Doing"], 1),
    ("Backlog", ["--after", "Done"], 2),
    ("Backlog", ["--before", "Done"], 1),
    ("Done", ["--after", "Backlog"], 1),
    ("Done", ["--before", "Backlog"], 0),
    ("Doing", ["--position", "0"], 0),
    ("Doing", ["--position", "2"], 2),
])
def test_move_uses_final_index(name, flags, index):
    r = _run(["move", name, *flags])
    assert r.returncode == 0, r.stderr
    pid = next(row["id"] for row in ROWS if row["name"] == name)
    assert _mutation(r) == [{"input": {"pipelineId": pid, "position": index}}]
    assert f"* {index}: {name}" in r.stdout
    assert "UpdatePipelineInput!" in r.stderr


@pytest.mark.parametrize("flags,extra", [([], {}), (["-d", "Notes"], {"description": "Notes"}),
                                         (["-d", ""], {"description": ""})])
def test_rename_sends_only_requested_fields(flags, extra):
    r = _run(["rename", "doing", "In Progress", *flags])
    assert r.returncode == 0, r.stderr
    assert _mutation(r) == [{"input": {"pipelineId": "p2", "name": "In Progress", **extra}}]
    assert "* 1: In Progress" in r.stdout


@pytest.mark.parametrize("verb,args", [
    ("create", ["DOING"]), ("rename", ["Backlog", "doing"]),
])
def test_duplicate_names_refuse(verb, args):
    r = _run([verb, *args])
    assert r.returncode != 0
    assert "already exists" in r.stderr
    assert not _mutation(r)


@pytest.mark.parametrize("args", [
    ["move", "Do", "--position", "0"], ["rename", "Do", "New"],
    ["create", "New", "--after", "Do"], ["move", "Backlog", "--before", "Do"],
])
def test_ambiguous_names_and_anchors_refuse_and_list_candidates(args):
    r = _run(args)
    assert r.returncode != 0
    assert "Ambiguous pipeline" in r.stderr
    assert "Doing" in r.stderr and "Done" in r.stderr
    assert not _mutation(r)


def test_exact_match_wins_over_partial_match():
    rows = ROWS + [{"id": "p4", "name": "Doing later", "issues": {"totalCount": 0}}]
    r = _run(["rename", "DOING", "Working"], rows=rows)
    assert r.returncode == 0, r.stderr
    assert _mutation(r) == [{"input": {"pipelineId": "p2", "name": "Working"}}]


@pytest.mark.parametrize("args,message", [
    (["create", "New", "extra"], "quote"),
    (["move", "Doing", "extra", "--position", "0"], "quote"),
    (["rename", "Doing", "New", "extra"], "quote"),
    (["delete", "Doing", "extra", "-y"], "quote"),
    (["create", "--bad"], "needs --"),
    (["create", "New", "--unknown"], "Unknown option"),
    (["move", "Doing"], "requires --after"),
    (["move", "Doing", "--after", "Doing"], "itself"),
    (["create", "New", "--after", "Done", "--before", "Doing"], "only one"),
    (["create", "New", "--position"], "requires a value"),
    (["create", "New", "-d"], "requires a value"),
    (["rename", "Doing"], "requires 2"),
    (["create", ""], "must not be empty"),
    (["rename", "Doing", "   "], "must not be empty"),
    (["move", "Missing", "--position", "0"], "not found"),
    (["delete", "Missing", "-y"], "not found"),
    (["create", "New", "--after", "Missing"], "not found"),
    (["move", "Doing", "-d", "notes"], "not valid"),
    (["rename", "Doing", "New", "--position", "0"], "not valid"),
    (["delete", "Doing", "--after", "Done"], "not valid"),
    (["create", "New", "-y"], "only valid"),
])
def test_invalid_arguments_mutate_nothing(args, message):
    r = _run(args)
    assert r.returncode != 0
    assert message in r.stderr
    assert not _mutation(r)


@pytest.mark.parametrize("verb", ["create", "move"])
@pytest.mark.parametrize("position", ["-1", "1.5", "abc", "4", "9999999999999999999999"])
def test_invalid_position_refuses(verb, position):
    r = _run([verb, "New" if verb == "create" else "Doing", "--position", position])
    assert r.returncode != 0
    assert "Invalid position" in r.stderr
    assert not _mutation(r)


def test_move_cannot_use_index_equal_to_original_length():
    r = _run(["move", "Doing", "--position", "3"])
    assert r.returncode != 0 and not _mutation(r)


@pytest.mark.parametrize("count", [1, 125, 10001])
def test_delete_refuses_nonempty_using_exact_unfiltered_count(count):
    rows = [dict(row) for row in ROWS]
    rows[1] = {**rows[1], "issues": {"totalCount": count, "nodes": []}}
    r = _run(["delete", "Doing", "-y"], rows=rows)
    assert r.returncode != 0
    assert f"{count} issues (exact count, including closed issues)" in r.stderr
    assert "issues { totalCount }" in r.stderr
    assert "state: OPEN" not in r.stderr
    assert not _mutation(r)


@pytest.mark.parametrize("issues", [None, {}, {"totalCount": None}, {"totalCount": -1}])
def test_delete_refuses_unavailable_exact_count(issues):
    rows = [{**row, "issues": issues} for row in ROWS]
    r = _run(["delete", "Doing", "-y"], rows=rows)
    assert r.returncode != 0
    assert "exact issue count is unavailable" in r.stderr
    assert not _mutation(r)


@pytest.mark.parametrize("name,dest", [("Backlog", "p2"), ("Doing", "p1"), ("Done", "p2")])
@pytest.mark.parametrize("flags", [[], ["-y"]])
def test_delete_empty_uses_adjacent_destination_and_prints_order(name, dest, flags):
    r = _run(["delete", name, *flags])
    assert r.returncode == 0, r.stderr
    pid = next(row["id"] for row in ROWS if row["name"] == name)
    assert _mutation(r) == [{"input": {"pipelineId": pid, "destinationPipelineId": dest}}]
    assert "API destination: adjacent pipeline" in r.stderr and "no issues to move" in r.stderr
    assert f"* Removed: {name}" in r.stdout and "Pipeline order (0-based)" in r.stdout
    assert "DeletePipelineInput!" in r.stderr


def test_delete_only_pipeline_refuses():
    r = _run(["delete", "Backlog", "-y"], rows=ROWS[:1])
    assert r.returncode != 0 and not _mutation(r)
    assert "only pipeline" in r.stderr


@pytest.mark.parametrize("name", ["--experimental", "--help"])
def test_flag_shaped_names_require_terminator_and_can_be_created(name):
    r = _run(["create", "--position", "0", "--", name])
    assert r.returncode == 0, r.stderr
    assert _mutation(r) == [{"input": {"workspaceId": "ws-gid", "name": name, "position": 0}}]


@pytest.mark.parametrize("verb", ["create", "move", "rename", "delete"])
def test_verb_named_pipeline_escape_reaches_view(verb):
    stubs = _stubs() + '\ncmd_pipeline_show() { printf "VIEW:%s\\n" "$@"; }'
    r = run_zh_with_stubs(stubs, 'main pipeline -- "$@"', args=[verb])
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"VIEW:--\nVIEW:{verb}\n"
    assert not _mutation(r)


@pytest.mark.parametrize("args", [["--help"], ["create", "--help"],
                                  ["move", "Doing", "--help"], ["rename", "--help"],
                                  ["delete", "--help"]])
def test_pipeline_help_needs_no_credentials(args):
    r = run_zh_with_stubs('load_config() { error "MUST NOT LOAD"; }',
                          'main pipeline "$@"', args=args)
    assert r.returncode == 0, r.stderr
    assert "Usage: zh pipeline" in r.stdout
    assert "zh pipeline -- create" in r.stdout
    assert "totalCount" in r.stdout and "Propose" in r.stdout


def test_missing_workspace_list_refuses_before_mutation():
    r = _run(["create", "New"], list_null=True)
    assert r.returncode != 0 and not _mutation(r)
    assert "Could not read" in r.stderr


def test_null_mutation_payload_does_not_report_success():
    r = _run(["move", "Doing", "--position", "0"], mutation_null=True)
    assert r.returncode != 0
    assert "did not confirm" in r.stderr
    assert "Pipeline order" not in r.stdout


def test_reread_failure_warns_against_retrying_successful_mutation():
    r = _run(["create", "New"], reread_fail=True)
    assert r.returncode != 0
    assert len(_mutation(r)) == 1
    assert "succeeded" in r.stderr and "before retrying" in r.stderr


@pytest.mark.parametrize("reply,yes,success", [("Doing\n", False, True),
                                              ("wrong\n", False, False),
                                              ("", True, True)])
def test_interactive_delete_retypes_name(tmp_path, reply, yes, success):
    master, slave = pty.openpty()
    script = (f'source {shlex.quote(str(Path(__file__).resolve().parents[1] / "zh"))}\n'
              + _stubs() + '\nmain pipeline delete Doing' + (' -y' if yes else ''))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NO_COLOR": "1"}
    try:
        proc = subprocess.Popen(["bash", "-c", script], stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        if reply:
            os.write(master, reply.encode())
        stdout, stderr = proc.communicate(timeout=15)
        assert (proc.returncode == 0) == success, stderr
        assert ("MUTATION=" in stderr) == success
        assert ("Type the pipeline name" in stderr) == (not yes)
        if not success:
            assert "Nothing was deleted" in stderr
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("verb", ["create", "move", "rename", "delete"])
def test_missing_names_report_usage_without_loading_config(verb):
    r = run_zh_with_stubs('load_config() { error "MUST NOT LOAD"; }',
                          'main pipeline "$@"', args=[verb])
    assert r.returncode != 0
    assert f"Usage: zh pipeline {verb}" in r.stderr
    assert "MUST NOT LOAD" not in r.stderr


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_help_flag_as_option_value_is_sent_literally(value):
    r = _run(["create", "New", "-d", value])
    assert r.returncode == 0, r.stderr
    assert "Usage: zh pipeline" not in r.stdout
    assert _mutation(r)[0]["input"]["description"] == value


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_help_flag_as_anchor_value_reaches_the_resolver(value):
    r = _run(["create", "New", "--after", value])
    assert r.returncode != 0
    assert "Usage: zh pipeline" not in r.stdout
    assert "Pipeline not found: " + value in r.stderr
    assert not _mutation(r)


def test_help_flag_still_prints_help_elsewhere():
    r = _run(["create", "New", "--after", "Doing", "-h"])
    assert r.returncode == 0 and "Usage: zh pipeline" in r.stdout
    assert not _mutation(r)


@pytest.mark.parametrize("name", ["one", "Do", "dON", "oin"])
def test_delete_refuses_partial_name_and_names_candidates(name):
    r = _run(["delete", name, "-y"])
    assert r.returncode != 0
    assert "No pipeline named exactly" in r.stderr
    assert not _mutation(r)
    if name == "one":
        assert "Done" in r.stderr


def test_delete_accepts_exact_name_case_and_whitespace_insensitive():
    r = _run(["delete", "  dOnE ", "-y"])
    assert r.returncode == 0, r.stderr
    assert _mutation(r)[0]["input"]["pipelineId"] == "p3"


def test_create_empty_workspace_and_empty_description():
    r = _run(["create", "First", "-d", ""], rows=[])
    assert r.returncode == 0, r.stderr
    assert _mutation(r) == [{"input": {"workspaceId": "ws-gid", "name": "First",
                                      "position": 0, "description": ""}}]
    assert "* 0: First" in r.stdout


def test_unique_partial_mutation_target_resolves():
    r = _run(["move", "back", "--before", "Doing"])
    assert r.returncode == 0, r.stderr
    assert _mutation(r) == [{"input": {"pipelineId": "p1", "position": 0}}]


@pytest.mark.parametrize("name", ["Create", "move", "rename", "delete", "--flag"])
def test_escaped_pipeline_names_reach_real_issue_listing(name):
    rows = ROWS + [{"id": "p4", "name": name, "issues": {"totalCount": 0}}]
    stubs = _stubs(rows) + '''
    zh_pipeline_issues_fetch_all() {
        echo "ISSUES_PIPELINE=$1" >&2
        printf '%s' '{"nodes":[],"complete":true,"totalCount":0}'
    }
    '''
    r = run_zh_with_stubs(stubs, 'main pipeline --all -- "$@"', args=[name])
    assert r.returncode == 0, r.stderr
    assert f"Pipeline: {name}" in r.stdout
    assert "ISSUES_PIPELINE=p4" in r.stderr
    assert not _mutation(r)


@pytest.mark.parametrize("args", [["Doing", "extra"], ["--", "Doing", "extra"]])
def test_view_rejects_extra_args(args):
    r = _run(args)
    assert r.returncode != 0
    assert "quote multi-word" in r.stderr
    assert not _mutation(r)


def test_interactive_delete_rechecks_count_after_confirmation(tmp_path):
    master, slave = pty.openpty()
    stubs = _stubs() + '''
    read() {
        reply=Doing
        jq 'map(if .id == "p2" then .issues.totalCount = 1 else . end)' \
            "$pipeline_state" > "$pipeline_state.next"
        mv "$pipeline_state.next" "$pipeline_state"
    }
    '''
    script = (f'source {shlex.quote(str(Path(__file__).resolve().parents[1] / "zh"))}\n'
              + stubs + '\nmain pipeline delete Doing')
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NO_COLOR": "1"}
    try:
        proc = subprocess.Popen(["bash", "-c", script], stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode != 0
        assert "no longer confirmed empty" in stderr
        assert "MUTATION=" not in stderr
    finally:
        os.close(master)
        os.close(slave)
