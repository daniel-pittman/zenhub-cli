"""Regression tests for mirror-derived aggregate counts (#96).

`board`, `count`, and `pipeline` derive their numbers from ZenHub's MIRROR of
GitHub issue state. Against a lapsed workspace the mirror keeps serving stale
values, so these commands return a plausible-looking number instead of an
error — and `count --json` asserted `exact: true` on top of it. Nobody
double-checks "24 open issues"; it reads as data, not as a claim.

Per the ticket's acceptance criteria these assert the QUALIFYING OUTPUT IS
PRESENT, not merely that an exit code changed: the pre-fix commands exit 0 with
a confident number, so an exit-code-only test passes against the bug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bash_runner import run_zh_with_stubs  # noqa: E402


def _stubs(zenhub_open: int, github_open, repos=("acme/widgets",)) -> str:
    """Workspace reporting `zenhub_open`; GitHub reporting `github_open`.
    github_open=None makes the gh search fail (uncoverable)."""
    pipes = ",".join(
        f'{{"name":"P{i}","issues":{{"totalCount":{n}}}}}'
        for i, n in enumerate([zenhub_open])
    )
    repo_nodes = ",".join(
        f'{{"ownerName":"{r.split("/")[0]}","name":"{r.split("/")[1]}"}}' for r in repos
    )
    gh_body = "        gh() { return 1; }\n" if github_open is None else (
        '        gh() {\n'
        '            if [[ "$*" == *search/issues* ]]; then printf "%s" "' + str(github_open) + '"; return 0; fi\n'
        '            return 0\n'
        '        }\n'
    )
    return f"""
        load_config() {{ :; }}
        get_repo_info() {{ printf 'acme/widgets'; }}
        get_repo_id() {{ printf 'repo-gid'; }}
        get_workspace_id() {{ printf 'ws-gid'; }}
        zh_graphql() {{
            local q="$1"
            if [[ "$q" == *repositoriesConnection* ]]; then
                printf '%s' '{{"data":{{"workspace":{{"repositoriesConnection":{{"nodes":[{repo_nodes}]}}}}}}}}'
            else
                printf '%s' '{{"data":{{"workspace":{{"name":"WS","pipelinesConnection":{{"nodes":[{pipes}]}}}}}}}}'
            fi
        }}
{gh_body}
    """


# --- the helper -------------------------------------------------------------

def test_more_open_in_zenhub_than_github_is_implausible():
    r = run_zh_with_stubs(_stubs(24, 0), "zh_count_plausibility_check ws-gid 24")
    out = json.loads(r.stdout)
    assert out["covered"] is True and out["plausible"] is False
    assert out["zenhub_open"] == 24 and out["github_open"] == 0


def test_fewer_open_in_zenhub_is_normal_and_stays_silent():
    """A board legitimately holds fewer open issues than its repos contain —
    warning here would fire forever on every workspace that doesn't board
    everything, which is most of them."""
    r = run_zh_with_stubs(_stubs(3, 90), "zh_count_plausibility_check ws-gid 3")
    out = json.loads(r.stdout)
    assert out["plausible"] is True


def test_equal_counts_are_plausible():
    r = run_zh_with_stubs(_stubs(5, 5), "zh_count_plausibility_check ws-gid 5")
    assert json.loads(r.stdout)["plausible"] is True


def test_uncoverable_is_not_reported_as_agreement():
    """Inability to check must not read as verified — the conflation #94's
    review caught, carried over deliberately."""
    r = run_zh_with_stubs(_stubs(24, None), "zh_count_plausibility_check ws-gid 24")
    out = json.loads(r.stdout)
    assert out["covered"] is False
    assert out["plausible"] is None, "must be null, never true"


def test_check_scopes_to_every_workspace_repo():
    """Multi-repo workspaces must compare against ALL their repos, or other
    repos' issues inflate the board side and the check false-positives."""
    r = run_zh_with_stubs(
        _stubs(10, 50, repos=("acme/widgets", "acme/gears", "acme/bolts")),
        "zh_count_plausibility_check ws-gid 10")
    out = json.loads(r.stdout)
    assert out["repos"] == 3 and out["plausible"] is True


# --- the three aggregate commands ------------------------------------------

def test_count_human_output_qualifies_a_stale_number():
    r = run_zh_with_stubs(_stubs(24, 0), "cmd_count")
    out = r.stdout + r.stderr
    assert "do NOT agree with GitHub" in out, f"missing qualifier: {out[-300:]!r}"


def test_count_json_is_additive_and_flags_untrustworthy():
    r = run_zh_with_stubs(_stubs(24, 0), "cmd_count --json")
    d = json.loads(r.stdout)
    assert d["total"] == 24
    assert d["exact"] is True, "`exact` keeps its original meaning (not a truncated page)"
    assert d["trustworthy"] is False, "the new key a consumer should branch on"
    assert d["mirror_check"]["github_open"] == 0


def test_count_json_trustworthy_is_null_when_uncheckable():
    r = run_zh_with_stubs(_stubs(24, None), "cmd_count --json")
    d = json.loads(r.stdout)
    assert d["trustworthy"] is None, "unknown must not collapse to false or true"


def test_count_quiet_keeps_stdout_a_bare_number():
    """`-q` feeds scripts; the caveat must go to stderr so the value stays
    pipeable."""
    r = run_zh_with_stubs(_stubs(24, 0), "cmd_count -q")
    assert r.stdout.strip() == "24"
    assert "does not agree" in r.stderr


def test_count_plausible_workspace_is_not_nagged():
    r = run_zh_with_stubs(_stubs(3, 90), "cmd_count")
    out = r.stdout + r.stderr
    assert "do NOT agree" not in out
    assert "Consistent with GitHub" in out


def test_board_qualifies_a_stale_number():
    r = run_zh_with_stubs(_stubs(24, 0), "cmd_board")
    out = r.stdout + r.stderr
    assert "do NOT agree with GitHub" in out, f"board did not qualify: {out[-300:]!r}"


def test_board_silent_when_plausible():
    r = run_zh_with_stubs(_stubs(3, 90), "cmd_board")
    assert "do NOT agree" not in (r.stdout + r.stderr)


def test_no_verify_skips_the_check_entirely():
    """Opt-out must not silently still call GitHub."""
    stubs = _stubs(24, 0).replace(
        'if [[ "$*" == *search/issues* ]]; then printf "%s" "0"; return 0; fi',
        'if [[ "$*" == *search/issues* ]]; then echo "SEARCHED" >&2; printf "%s" "0"; return 0; fi')
    r = run_zh_with_stubs(stubs, "cmd_count --no-verify")
    assert "SEARCHED" not in r.stderr, "--no-verify still hit GitHub"
    assert "do NOT agree" not in (r.stdout + r.stderr)


# --- review findings on #108 ------------------------------------------------

def _many_repo_stubs(n_repos: int, per_batch: int, fail_after: int = -1) -> str:
    """Workspace with `n_repos` repos; each gh search returns `per_batch`.
    fail_after >= 0 makes the (fail_after+1)-th search fail."""
    repos = ",".join(f'{{"ownerName":"acme","name":"repo-with-a-longish-name-{i:03d}"}}'
                     for i in range(n_repos))
    fail = "" if fail_after < 0 else (
        f'                if [[ $n -gt {fail_after} ]]; then return 1; fi\n')
    return f"""
        load_config() {{ :; }}
        get_repo_info() {{ printf 'acme/widgets'; }}
        get_workspace_id() {{ printf 'ws-gid'; }}
        zh_graphql() {{
            local q="$1"
            if [[ "$q" == *repositoriesConnection* ]]; then
                printf '%s' '{{"data":{{"workspace":{{"repositoriesConnection":{{"nodes":[{repos}]}}}}}}}}'
            else
                printf '%s' '{{"data":{{"workspace":{{"name":"WS","pipelinesConnection":{{"nodes":[{{"name":"P","issues":{{"totalCount":1}}}}]}}}}}}}}'
            fi
        }}
        gh() {{
            if [[ "$*" == *search/issues* ]]; then
                local n
                n=$(cat /tmp/zh_search_calls 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > /tmp/zh_search_calls
                # every batch must stay under GitHub's 256-char query cap
                local q="$*"; if [[ ${{#q}} -gt 300 ]]; then echo "QUERY_TOO_LONG" >&2; return 1; fi
{fail}
                printf '%s' '{per_batch}'
                return 0
            fi
            return 0
        }}
    """


def test_large_workspace_is_chunked_and_summed():
    """GitHub caps a search query at 256 chars, so a workspace past ~10 repos
    overflows a single request. Batching keeps every query legal and the sum
    exact, because the batches are disjoint repository sets."""
    import subprocess
    subprocess.run(["rm", "-f", "/tmp/zh_search_calls"])
    r = run_zh_with_stubs(_many_repo_stubs(24, 10), "zh_count_plausibility_check ws-gid 5")
    out = json.loads(r.stdout)
    calls = int(open("/tmp/zh_search_calls").read().strip())
    assert calls > 1, "24 repos must not be crammed into one over-long query"
    assert out["covered"] is True
    assert out["github_open"] == calls * 10, "batch totals must be summed"
    assert out["repos"] == 24
    assert "QUERY_TOO_LONG" not in r.stderr


def test_a_failed_batch_invalidates_the_whole_check():
    """A partial sum understates GitHub and would manufacture a false
    'ZenHub has more' verdict — worse than not checking."""
    import subprocess
    subprocess.run(["rm", "-f", "/tmp/zh_search_calls"])
    r = run_zh_with_stubs(_many_repo_stubs(24, 10, fail_after=1),
                          "zh_count_plausibility_check ws-gid 5")
    out = json.loads(r.stdout)
    assert out["covered"] is False
    assert out["plausible"] is None, "must not report a verdict from a partial sum"


def test_pipeline_qualifies_a_stale_listing():
    """cmd_pipeline carries its own query and output string, so it needs its
    own assertion rather than inheriting the helper's coverage."""
    stubs = _stubs(24, 0).replace(
        "            else\n                printf '%s'",
        "            elif [[ \"$q\" == *searchIssuesByPipeline* ]]; then\n"
        "                printf '%s' '{\"data\":{\"searchIssuesByPipeline\":{\"totalCount\":1,\"pageInfo\":{\"hasNextPage\":false,\"endCursor\":null},\"nodes\":[{\"number\":5,\"title\":\"x\",\"state\":\"OPEN\",\"zenhubUrl\":\"u\",\"estimate\":null,\"assignees\":{\"nodes\":[]},\"repository\":{\"ownerName\":\"acme\",\"name\":\"widgets\"}}]}}}'\n"
        "            else\n                printf '%s'", 1)
    stubs = stubs.replace('{"name":"P0","issues":{"totalCount":24}}',
                          '{"id":"p1","name":"P0","issues":{"totalCount":24}}')
    r = run_zh_with_stubs(stubs, 'cmd_pipeline "P0"')
    out = r.stdout + r.stderr
    # board/count say "do NOT agree"; pipeline says "does NOT agree" — match both.
    assert "NOT agree with GitHub" in out, f"pipeline did not qualify: {out[-400:]!r}"
    assert "zh doctor" in out, "should route to the connection check"
