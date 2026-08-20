"""Regression tests for ZenHub connection diagnosis (#99).

A repo that was NEVER registered with ZenHub and one whose authorization
LAPSED present identically through the API: reads succeed, the board renders,
counts look plausible, and every state change made on GitHub is silently
discarded. `zh doctor` reported the symptom (stale mirror) but told the
operator to "re-authorize" — which is the wrong fix for the far more common
never-connected case, and cost an hour of dead-end debugging.

The discriminator is whether GitHub can actually DELIVER events to ZenHub,
visible on the repo's webhooks. These pin that classification, and — most
importantly — that an unreadable hook list reports `unknown`, never `connected`
(v2.1.0's rule: do not report a health you cannot verify).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bash_runner import run_zh_with_stubs  # noqa: E402


def _gh_stub(payload: str, rc: int = 0) -> str:
    """Stub `gh api repos/*/hooks` with a canned payload (or a failure)."""
    return f"""
        gh() {{
            if [[ "$*" == *hooks* ]]; then
                cat <<'JSON'
{payload}
JSON
                return {rc}
            fi
            return 0
        }}
    """


HEALTHY = json.dumps([
    {"id": 1, "created_at": "2026-01-29T00:00:00Z",
     "config": {"url": "https://webhook.zenhub.com/webhook/github/v2"},
     "last_response": {"code": 202, "status": "active"}},
])

# The real-world broken shape: an orphaned hook GitHub still delivers to and
# ZenHub rejects. Age is NOT the signal — this one is older than some healthy
# hooks — so classification must read last_response only.
REJECTED = json.dumps([
    {"id": 2, "created_at": "2026-01-14T00:00:00Z",
     "config": {"url": "https://webhook.zenhub.com/webhook/github/v2"},
     "last_response": {"code": 422, "status": "misconfigured"}},
    {"id": 3, "created_at": "2026-05-26T00:00:00Z",
     "config": {"url": "https://webhook.zenhub.com/webhook/github/v2"},
     "last_response": {"code": 422, "status": "misconfigured"}},
])

# A repo that recovered: the orphan is still there, but a working hook exists.
MIXED = json.dumps([
    {"id": 2, "created_at": "2026-01-14T00:00:00Z",
     "config": {"url": "https://webhook.zenhub.com/webhook/github/v2"},
     "last_response": {"code": 422, "status": "misconfigured"}},
    {"id": 4, "created_at": "2026-08-20T00:00:00Z",
     "config": {"url": "https://webhook.zenhub.com/webhook/github/v2"},
     "last_response": {"code": 202, "status": "active"}},
])

NO_ZENHUB_HOOK = json.dumps([
    {"id": 9, "created_at": "2026-01-01T00:00:00Z",
     "config": {"url": "https://example.com/other"},
     "last_response": {"code": 200, "status": "active"}},
])

PENDING = json.dumps([
    {"id": 5, "created_at": "2026-08-20T00:00:00Z",
     "config": {"url": "https://webhook.zenhub.com/webhook/github/v2"},
     "last_response": {"code": None, "status": "unused"}},
])


def _state(payload: str):
    r = run_zh_with_stubs(_gh_stub(payload), "zh_connection_check acme/widgets")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_healthy_hook_reports_connected():
    out = _state(HEALTHY)
    assert out["state"] == "connected"
    assert out["healthy"] == 1


def test_all_hooks_rejected_reports_not_registered():
    """The observed failure: hooks exist, GitHub delivers, ZenHub 422s them."""
    out = _state(REJECTED)
    assert out["state"] == "not_registered"
    assert out["rejected"] == 2 and out["healthy"] == 0


def test_one_working_hook_wins_over_orphans():
    """After reconnecting, the stale 422 orphan remains — it must not mask the
    working hook and report the repo as broken."""
    out = _state(MIXED)
    assert out["state"] == "connected"
    assert out["healthy"] == 1 and out["rejected"] == 1


def test_no_zenhub_hook_reports_not_registered():
    out = _state(NO_ZENHUB_HOOK)
    assert out["state"] == "not_registered"
    assert "no ZenHub webhook" in out["reason"]


def test_undelivered_hook_is_unknown_not_broken():
    """A hook that has never fired is genuinely unknown — do not call it dead."""
    out = _state(PENDING)
    assert out["state"] == "unknown"


def test_unreadable_hooks_report_unknown_never_connected():
    """THE safety property: a non-admin gets 403 on the hooks endpoint. That
    must degrade to `unknown` — reporting `connected` would assert a health we
    did not verify (the v2.1.0 rule)."""
    stubs = """
        gh() { if [[ "$*" == *hooks* ]]; then return 1; fi; return 0; }
    """
    r = run_zh_with_stubs(stubs, "zh_connection_check acme/widgets")
    out = json.loads(r.stdout)
    assert out["checked"] is False
    assert out["state"] == "unknown"
    assert out["state"] != "connected"


# --- doctor integration: the stale finding must NAME its cause ---------------

_DOCTOR_STALE = r"""
    load_config() { :; }
    get_repo_info() { printf 'acme/widgets'; }
    get_repo_id() { printf 'repo-gid'; }
    get_workspace_id() { printf 'ws-gid'; }
    # One open-in-ZenHub issue that GitHub reports closed -> mirror is stale.
    zh_graphql() {
        printf '%s' '{"data":{"workspace":{"issues":{"totalCount":1,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
            {"number":7,"title":"stale one","state":"OPEN","repository":{"ownerName":"acme","name":"widgets"},"parentIssue":null}
        ]}}}}'
    }
"""


def _doctor_with_hooks(hooks_payload: str, gh_fails: bool = False):
    if gh_fails:
        gh = '        gh() { if [[ "$*" == *hooks* ]]; then return 1; fi; printf "%s" "CLOSED"; }\n'
    else:
        gh = (
            '        gh() {\n'
            '            if [[ "$*" == *hooks* ]]; then cat <<\'JSON\'\n'
            f'{hooks_payload}\n'
            'JSON\n'
            '                return 0\n'
            '            fi\n'
            '            if [[ "$*" == *"issue list"* ]]; then printf "%s" \'[{"number":7,"state":"CLOSED"}]\'; return 0; fi\n'
            '            return 0\n'
            '        }\n'
        )
    return run_zh_with_stubs(_DOCTOR_STALE + gh, "cmd_doctor || true")


def test_stale_board_blames_missing_registration_not_expiry():
    """The whole point of #99: when the repo isn't registered, doctor must say
    so and point at Manage Repositories — NOT tell the operator to
    re-authorize, which is the wrong fix and burns the investigation."""
    r = _doctor_with_hooks(REJECTED)
    out = r.stdout + r.stderr
    assert "NOT receiving GitHub events" in out, out[-500:]
    assert "Manage Repositories" in out


def test_connected_repo_is_not_told_to_reconnect():
    """With a working hook, a stale board is a lag — don't send them to
    Manage Repositories for a connection that is fine."""
    r = _doctor_with_hooks(HEALTHY)
    out = r.stdout + r.stderr
    assert "receiving GitHub events" in out
    assert "Manage Repositories" not in out


def test_doctor_reports_unknown_connection_without_claiming_health():
    r = _doctor_with_hooks("", gh_fails=True)
    out = r.stdout + r.stderr
    assert "Connection state unknown" in out
    assert "is receiving GitHub events" not in out
