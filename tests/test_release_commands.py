"""Tests for the `zh release` / `zh releases` commands.

Drives the real bash `cmd_release*` functions through `run_zh_with_stubs`.
The `zh_graphql` stub answers by query shape and echoes each mutation's
variables to stderr (MUTATION=...) so a test can assert exactly what was
sent, and that nothing was sent when input validation should refuse first.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bash_runner import run_zh_with_stubs  # noqa: E402


def _release(rid: str, title: str, *, state: str = "OPEN",
             start: str = "2026-03-01", end: str = "2026-03-31",
             issues: int = 0) -> dict:
    return {
        "id": rid, "title": title, "description": None, "state": state,
        "startOn": start, "endOn": end, "closedAt": None, "issuesCount": issues,
        "repositories": {"nodes": [{"ownerName": "acme", "name": "widgets"}]},
    }


_DEFAULT_RELEASES = [
    _release("rel-1", "Spring launch", issues=2),
    _release("rel-2", " Padded title ", start="2026-01-01", end="2026-01-31"),
    _release("rel-3", "Old release", state="CLOSED"),
]


def _stubs(releases: list[dict] | None = None) -> str:
    releases = _DEFAULT_RELEASES if releases is None else releases
    list_resp = json.dumps({"data": {"workspace": {
        "name": "TestWS",
        "releases": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                     "nodes": releases},
    }}})
    by_id = {r["id"]: r for r in releases}
    detail_cases = "\n".join(
        f"            *'\"{rid}\"'*) printf '%s' '{json.dumps({'data': {'node': r}})}' ;;"
        for rid, r in by_id.items()
    )
    issues_resp = json.dumps({"data": {"node": {"issues": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {"number": 7, "title": "Open thing", "state": "OPEN",
             "htmlUrl": "https://github.com/acme/widgets/issues/7",
             "estimate": {"value": 2}, "assignees": {"nodes": [{"login": "alice"}]},
             "repository": {"ownerName": "acme", "name": "widgets"},
             "pipelineIssues": {"nodes": [{"pipeline": {"name": "In Progress"}}]}},
            {"number": 8, "title": "Done thing", "state": "CLOSED",
             "htmlUrl": "https://github.com/acme/widgets/issues/8",
             "estimate": None, "assignees": {"nodes": []},
             "repository": {"ownerName": "acme", "name": "widgets"},
             "pipelineIssues": {"nodes": []}},
        ],
    }}}})
    return f"""
    load_config() {{ :; }}
    get_repo_info() {{ printf 'acme/widgets'; }}
    get_workspace_id() {{ printf 'ws-gid'; }}
    resolve_gh_repo_id() {{ printf '4242'; }}
    zh_graphql() {{
        local q="$1" v="${{2:-}}"
        if [[ "$q" == *createRelease* ]]; then
            echo "MUTATION=$(printf '%s' "$v" | jq -c .)" >&2
            printf '%s' '{{"data":{{"createRelease":{{"release":{{"id":"rel-new","title":"T","state":"OPEN","startOn":"2026-05-01","endOn":"2026-05-31","repositories":{{"nodes":[]}}}}}}}}}}'
        elif [[ "$q" == *updateRelease* ]]; then
            echo "MUTATION=$(printf '%s' "$v" | jq -c .)" >&2
            local st
            st=$(printf '%s' "$v" | jq -r '.input.release.state // "OPEN"')
            printf '{{"data":{{"updateRelease":{{"release":{{"id":"x","title":"T","state":"%s","startOn":"2026-03-01","endOn":"2026-03-31","closedAt":null}}}}}}}}' "$st"
        elif [[ "$q" == *"releases(first"* ]]; then
            echo "LIST_VARS=$(printf '%s' "$v" | jq -c .)" >&2
            printf '%s' '{list_resp}'
        elif [[ "$q" == *"issues(first"* ]]; then
            printf '%s' '{issues_resp}'
        else
            case "$v" in
{detail_cases}
            *) printf '%s' '{{"data":{{"node":null}}}}' ;;
            esac
        fi
    }}
"""


def _mutation(stderr: str) -> dict | None:
    for line in stderr.splitlines():
        if line.startswith("MUTATION="):
            return json.loads(line[len("MUTATION="):])
    return None


# --- list --------------------------------------------------------------------

def test_releases_lists_open_by_default() -> None:
    r = run_zh_with_stubs(_stubs(), "cmd_releases")
    assert r.returncode == 0, r.stderr
    assert '"state":{"eq":"OPEN"}' in r.stderr
    assert "Spring launch" in r.stdout
    assert "2 issues" in r.stdout


def test_releases_all_sends_no_state_filter() -> None:
    r = run_zh_with_stubs(_stubs(), "cmd_releases --all")
    assert r.returncode == 0, r.stderr
    assert '"state":null' in r.stderr
    assert "(3 total)" in r.stdout


def test_releases_json_output_is_parseable() -> None:
    r = run_zh_with_stubs(_stubs(), "cmd_releases --json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert [rel["id"] for rel in doc["releases"]] == ["rel-1", "rel-2", "rel-3"]


def test_releases_rejects_unknown_option() -> None:
    r = run_zh_with_stubs(_stubs(), "cmd_releases --bogus")
    assert r.returncode != 0
    assert "Unknown option" in r.stderr


# --- show --------------------------------------------------------------------

def test_show_matches_title_case_insensitively() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_show "spring LAUNCH" --no-urls')
    assert r.returncode == 0, r.stderr
    assert "Release: Spring launch" in r.stdout
    assert "1 open, 1 closed" in r.stdout
    assert "#7" in r.stdout and "#8" in r.stdout


def test_show_ignores_surrounding_whitespace_in_titles() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_show "padded title"')
    assert r.returncode == 0, r.stderr
    assert "rel-2" in r.stdout


def test_show_accepts_release_id() -> None:
    r = run_zh_with_stubs(_stubs(), "cmd_release_show rel-3 --json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["title"] == "Old release"
    assert [i["number"] for i in doc["issues"]] == [7, 8]


def test_show_unknown_release_lists_available() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_show "Nope"')
    assert r.returncode == 1
    assert "Release 'Nope' not found" in r.stderr
    assert "Spring launch (OPEN)" in r.stderr


def test_show_duplicate_title_asks_for_id() -> None:
    dupes = [_release("rel-a", "Same"), _release("rel-b", "same")]
    r = run_zh_with_stubs(_stubs(dupes), 'cmd_release_show "Same"')
    assert r.returncode == 1
    assert "2 releases are titled 'Same'" in r.stderr
    assert "rel-a" in r.stderr and "rel-b" in r.stderr


# --- create ------------------------------------------------------------------

def test_create_sends_title_dates_and_repo() -> None:
    r = run_zh_with_stubs(
        _stubs(),
        'cmd_release_create "Summer launch" --start 2026-05-01 --end 2026-05-31 -d "Notes"',
    )
    assert r.returncode == 0, r.stderr
    assert _mutation(r.stderr) == {"input": {"release": {
        "title": "Summer launch", "startOn": "2026-05-01", "endOn": "2026-05-31",
        "repositoryGhIds": [4242], "description": "Notes",
    }}}
    assert "Created release: Summer launch" in r.stdout


def test_create_omits_empty_description_and_quiet_prints_id() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_create "X" -s 2026-05-01 -e 2026-05-01 -q')
    assert r.returncode == 0, r.stderr
    assert "description" not in _mutation(r.stderr)["input"]["release"]
    assert r.stdout.strip() == "rel-new"


def test_create_requires_both_dates() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_create "X" --start 2026-05-01')
    assert r.returncode != 0
    assert "Usage: zh release create" in r.stderr
    assert _mutation(r.stderr) is None


def test_create_rejects_malformed_date() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_create "X" --start 2026-13-01 --end 2026-12-31')
    assert r.returncode != 0
    assert "Invalid --start date" in r.stderr
    assert _mutation(r.stderr) is None


def test_create_rejects_start_after_end() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_create "X" --start 2026-06-02 --end 2026-06-01')
    assert r.returncode != 0
    assert "is after the end date" in r.stderr
    assert _mutation(r.stderr) is None


def test_create_refuses_unknown_flag() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_create "X" --stat 2026-06-01 --end 2026-06-02')
    assert r.returncode != 0
    assert "Unknown option for release create: --stat" in r.stderr
    assert _mutation(r.stderr) is None


# --- edit --------------------------------------------------------------------

def test_edit_sends_only_changed_fields() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_edit "Spring launch" --end 2026-04-07')
    assert r.returncode == 0, r.stderr
    assert _mutation(r.stderr) == {"input": {"releaseId": "rel-1", "release": {"endOn": "2026-04-07"}}}
    assert "Updated release: Spring launch" in r.stdout


def test_edit_can_clear_description_and_rename() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_edit rel-1 -t "Renamed" -d ""')
    assert r.returncode == 0, r.stderr
    assert _mutation(r.stderr)["input"]["release"] == {"title": "Renamed", "description": ""}


def test_edit_checks_new_end_against_existing_start() -> None:
    # Spring launch starts 2026-03-01; an end before that is refused.
    r = run_zh_with_stubs(_stubs(), 'cmd_release_edit "Spring launch" --end 2026-02-01')
    assert r.returncode != 0
    assert "would be after the end date" in r.stderr
    assert _mutation(r.stderr) is None


def test_edit_without_changes_shows_usage() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_edit "Spring launch"')
    assert r.returncode != 0
    assert "Usage: zh release edit" in r.stderr
    assert _mutation(r.stderr) is None


def test_edit_refuses_empty_title() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_edit "Spring launch" -t ""')
    assert r.returncode != 0
    assert "can't be empty" in r.stderr
    assert _mutation(r.stderr) is None


# --- close / reopen / delete ---------------------------------------------------

def test_close_sets_state_closed() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_close "Spring launch"')
    assert r.returncode == 0, r.stderr
    assert _mutation(r.stderr) == {"input": {"releaseId": "rel-1", "release": {"state": "CLOSED"}}}
    assert "Release CLOSED: Spring launch" in r.stdout


def test_reopen_sets_state_open() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_reopen "Old release"')
    assert r.returncode == 0, r.stderr
    assert _mutation(r.stderr) == {"input": {"releaseId": "rel-3", "release": {"state": "OPEN"}}}


def test_delete_explains_and_sends_nothing() -> None:
    r = run_zh_with_stubs(_stubs(), 'cmd_release_delete "Spring launch"')
    assert r.returncode == 1
    assert "no way to delete a release" in r.stderr
    assert "zh release close" in r.stderr
    assert _mutation(r.stderr) is None


# --- dispatch ------------------------------------------------------------------

def test_bare_release_lists_and_title_shows() -> None:
    listed = run_zh_with_stubs(_stubs(), "main release")
    assert listed.returncode == 0, listed.stderr
    assert "Releases in TestWS" in listed.stdout

    shown = run_zh_with_stubs(_stubs(), 'main rel "Spring launch" --no-urls')
    assert shown.returncode == 0, shown.stderr
    assert "Release: Spring launch" in shown.stdout
