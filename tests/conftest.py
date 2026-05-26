"""pytest fixtures + sys.path setup for the zh test suite.

The Python modules under test (`zh_api`, `zh_graphql_ops`) live as flat
files at the repo root rather than in an installed package. Adding the
repo root to sys.path here means `import zh_api` works in every test.

We also set `ZH_MCP_SKIP_BOOTSTRAP=1` before any test module imports
`mcp_server`. The MCP server's normal import path builds a venv at
`/tmp/zhenv` and `os.execv`s into it — which would mid-flight replace
the pytest process. The sentinel keeps the bootstrap dormant and
substitutes a no-op `FastMCP` stub so tests can exercise the tool
functions' guards and result shapes without pulling in mcp / torch /
transformers / numpy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Set BEFORE the sys.path tweak so any later `import mcp_server` from a
# test sees it in its environment.
os.environ.setdefault("ZH_MCP_SKIP_BOOTSTRAP", "1")

# Repo root is the parent of this `tests/` directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
