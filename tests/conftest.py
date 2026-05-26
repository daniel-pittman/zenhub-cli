"""pytest fixtures + sys.path setup for the zh test suite.

The Python modules under test (`zh_api`, `zh_graphql_ops`) live as flat
files at the repo root rather than in an installed package. Adding the
repo root to sys.path here means `import zh_api` works in every test.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root is the parent of this `tests/` directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
