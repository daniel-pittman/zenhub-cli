"""pytest fixtures + sys.path setup for the zh test suite.

The Python modules under test (`zh_api`, `zh_graphql_ops`) live as flat
files at the repo root rather than in an installed package. Adding the
repo root to sys.path here means `import zh_api` works in every test.

We also set `ZH_MCP_SKIP_BOOTSTRAP=1` before any test module imports
`mcp_server`. The MCP server's normal import path validates (and
builds, if missing or broken) a venv under `$XDG_DATA_HOME/zh/venv`
and then `os.execv`s into it — which would mid-flight replace the
pytest process. The sentinel keeps the bootstrap dormant and
substitutes a no-op `FastMCP` stub so tests can exercise the tool
functions' guards and result shapes without pulling in mcp / torch /
transformers / numpy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Set BEFORE the sys.path tweak so any later `import mcp_server` from a
# test sees it in its environment. Use direct assignment (not
# `setdefault`) so a stale `ZH_MCP_SKIP_BOOTSTRAP=0` in the developer's
# shell can't sneak past and trigger a real venv build mid-test.
os.environ["ZH_MCP_SKIP_BOOTSTRAP"] = "1"

# Repo root is the parent of this `tests/` directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _no_live_github(monkeypatch):
    """Keep the closed-parent guard's GitHub lookup off the network.

    `add_sub_issues` consults GitHub for the parent's real open/closed state
    (#92: ZenHub's mirror can lapse and report a closed issue as OPEN). Left
    unpatched, every add_sub_issues test would shell out to `gh auth token`
    and hit api.github.com: slow, network-dependent, and rate-limited in CI.

    None means "unknown", which is the fail-soft path: the guard falls back to
    the ZenHub state the test's own fixtures supply, so pre-#92 tests keep
    exercising exactly what they always did. Tests that assert on the GitHub
    side re-patch this with their own value.
    """
    import zh_graphql_ops
    monkeypatch.setattr(
        zh_graphql_ops, "get_gh_issue_state", lambda *a, **k: None
    )
