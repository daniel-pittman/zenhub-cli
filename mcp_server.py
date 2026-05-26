#!/usr/bin/env python3
"""
ZenHub MCP server — exposes ZenHub backlog operations to any Claude Code session
on this machine via the `zh` CLI.

Wraps tools/zh so that:
  - Read tools (board, pipeline, issue, mine, epic_list, epic_show,
    subissue_list) return structured data parseable by callers.
  - Write tools (create_issue, close_issue, move_issue, reorder, comment,
    epic_create, epic_update, epic_add_children, epic_remove_children,
    epic_close, epic_reopen, subissue_add_children, subissue_remove_children,
    subissue_reorder, assign, unassign, estimate) are explicit verbs
    so callers can audit which destructive operations they invoked.

Every tool optionally accepts a `repo_path` argument — the absolute path of a
git checkout that the underlying `zh` invocation runs from. This is required
because `zh` detects the GitHub repo via `git config --get remote.origin.url`
from its working directory. If omitted, falls back to:
  1. ZH_DEFAULT_REPO_PATH environment variable
  2. The MCP server's current working directory at launch time

Run as a subprocess (stdio transport):
    /usr/bin/python3 mcp_server.py

The script self-bootstraps /tmp/zhenv (mcp package) on first run and on
reboot when /tmp is wiped, then re-execs under that venv. Any python3 on PATH
that can run `python3 -m venv` works as the launcher.

Register user-scope so every Claude Code session sees it:
    claude mcp add --scope user zenhub \\
        /usr/bin/python3 \\
        /path/to/zenhub-cli/mcp_server.py

Environment overrides:
  ZH_DEFAULT_REPO_PATH — default git-checkout dir to run zh from
                         (otherwise uses MCP server cwd at launch)
  ZH_BIN_PATH          — path to zh bash script (default: peer to this file)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Self-bootstrap: /tmp gets wiped on reboot. If our venv is missing, build it
# with stdlib-only code and re-exec under it. Must run before any third-party
# import (mcp).
# -----------------------------------------------------------------------------

_VENV_DIR = Path("/tmp/zhenv")
_VENV_PY = _VENV_DIR / "bin" / "python3"
_VENV_DEPS = (
    "mcp",
    # similarity search: sentence-transformers brings in torch + transformers
    # + huggingface_hub. The model weights themselves are cached under
    # ~/.cache/huggingface/ so they survive reboots even when /tmp/zhenv
    # is wiped.
    "sentence-transformers",
    "numpy",
)
_VENV_MIN_PY = (3, 10)  # mcp package requires >= 3.10


def _find_builder_python() -> str:
    """Return a python3 executable suitable for building the venv.

    Prefer the interpreter that invoked us; fall back to common Homebrew /
    pyenv locations. Skips anything below `_VENV_MIN_PY`.
    """
    import shutil
    if sys.version_info >= _VENV_MIN_PY:
        return sys.executable
    candidates = [
        "/opt/homebrew/opt/pyenv/shims/python3",
        os.path.expanduser("~/.pyenv/shims/python3"),
        shutil.which("python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    probe = (
        "import sys; "
        f"sys.exit(0 if sys.version_info >= {_VENV_MIN_PY} else 1)"
    )
    seen = set()
    for cand in candidates:
        if not cand or cand in seen or not os.path.exists(cand):
            continue
        seen.add(cand)
        try:
            subprocess.check_call(
                [cand, "-c", probe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return cand
        except (subprocess.CalledProcessError, OSError):
            continue
    raise RuntimeError(
        f"No python3 >= {_VENV_MIN_PY[0]}.{_VENV_MIN_PY[1]} found to build "
        f"{_VENV_DIR}; install one (e.g. via pyenv or `brew install python`) "
        f"and retry."
    )


def _bootstrap_venv() -> None:
    if not _VENV_PY.exists():
        builder = _find_builder_python()
        # Log to stderr so MCP stdio transport isn't corrupted.
        print(
            f"[zenhub-mcp] bootstrapping {_VENV_DIR} with {builder}",
            file=sys.stderr,
        )
        subprocess.check_call([builder, "-m", "venv", str(_VENV_DIR)])
        subprocess.check_call(
            [str(_VENV_PY), "-m", "pip", "install",
             "--quiet", "--no-cache-dir", "--upgrade", "pip"]
        )
        subprocess.check_call(
            [str(_VENV_PY), "-m", "pip", "install",
             "--quiet", "--no-cache-dir", *_VENV_DEPS]
        )
    if os.path.realpath(sys.executable) != os.path.realpath(str(_VENV_PY)):
        os.execv(str(_VENV_PY), [str(_VENV_PY), __file__, *sys.argv[1:]])


# Test-mode escape hatch: setting ZH_MCP_SKIP_BOOTSTRAP=1 in the
# environment skips the venv bootstrap AND substitutes a minimal
# `FastMCP` stub for the import below. This lets the pytest suite
# exercise the guard logic and result-dict shapes in MCP tools
# without pulling in mcp / torch / transformers / numpy. Production
# (the actual MCP server transport) must NEVER set this — without
# the real FastMCP, the server doesn't serve.
_MCP_SKIP_BOOTSTRAP = os.environ.get("ZH_MCP_SKIP_BOOTSTRAP", "") == "1"

if not _MCP_SKIP_BOOTSTRAP:
    _bootstrap_venv()
    from mcp.server.fastmcp import FastMCP
else:
    # Minimal no-op stub. `@mcp.tool()` returns the function unchanged
    # so tests can call the wrapped tool directly. The stub class is
    # callable as `FastMCP("name")` and exposes a `.run()` that just
    # raises (we don't want a test accidentally launching a server).
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self, *args, **kwargs):  # noqa: ARG002
            def _decorator(fn):
                return fn
            return _decorator

        def run(self) -> None:
            raise RuntimeError(
                "FastMCP stub: ZH_MCP_SKIP_BOOTSTRAP is set. The MCP "
                "server cannot run in this mode; it's for unit tests only."
            )

# =============================================================================
# Paths and configuration
# =============================================================================

HERE = Path(__file__).resolve().parent
ZH_BIN = Path(os.environ.get("ZH_BIN_PATH", str(HERE / "zh")))

# Make sibling modules (zh_api.py, zh_graphql_ops.py, similarity.py)
# importable when the MCP server is launched via the bootstrapped venv
# from any cwd. sys.path may not include __file__'s directory in that
# case.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# ANSI color code regex — zh emits colored output for terminals; strip for MCP.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# =============================================================================
# Helpers
# =============================================================================

def _resolve_cwd(repo_path: str = "") -> str:
    """Resolve the working directory zh should run from.

    Priority:
      1. Explicit `repo_path` arg (if non-empty)
      2. ZH_DEFAULT_REPO_PATH env var
      3. Current working directory at server launch time
    """
    if repo_path:
        return repo_path
    return os.environ.get("ZH_DEFAULT_REPO_PATH", os.getcwd())


def _run_zh(args: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: float = 60.0) -> dict:
    """Invoke zh as a subprocess; return structured result.

    `timeout` is a soft cap on the whole invocation. ZenHub GraphQL
    queries typically return in < 5s; we cap at 60s to avoid the MCP
    server hanging if the API is unresponsive — review note.
    """
    if not ZH_BIN.exists():
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"zh binary not found at {ZH_BIN}",
            "stdout_plain": "",
        }
    try:
        result = subprocess.run(
            [str(ZH_BIN)] + args,
            capture_output=True,
            text=True,
            input=stdin,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": e.stdout or "",
            "stderr": (
                f"zh subprocess timed out after {timeout}s "
                f"(args={args!r})"
            ),
            "stdout_plain": _ANSI_RE.sub("", e.stdout or ""),
        }
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_plain": _ANSI_RE.sub("", result.stdout),
    }


def _parse_board(plain: str) -> dict:
    """Parse `zh board` plain output into {pipeline_name: count}."""
    # Lines look like: "  Product Backlog            44 ██████████████…"
    # Bar can be any combination of block/space chars AND may end in `…` when
    # truncated to terminal width. Match name + count; ignore everything after.
    out = {}
    for line in plain.splitlines():
        m = re.match(r"^\s+(\S.*?)\s+(\d+)(?:\s|$)", line)
        if m:
            name = m.group(1).strip()
            count = int(m.group(2))
            # Skip the "Board: <Workspace Name> (NN open issues)" header line.
            if name.startswith("Board:") or "open issues" in name:
                continue
            out[name] = count
    return out


def _parse_pipeline_listing(plain: str) -> list[dict]:
    """Parse `zh pipeline <name>` output into list of {number, repo, points, assignee, title}."""
    issues = []
    # Pattern: "  #NNN │ owner/repo │ N pts │ assignee"
    # Followed by indented title on next line(s)
    lines = plain.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(
            r"^\s*#(\d+)\s*│\s*(\S+/\S+)\s*│\s*(\S+)\s*pts\s*│\s*(\S+)\s*$",
            lines[i],
        )
        if m:
            number = int(m.group(1))
            repo = m.group(2)
            pts = m.group(3)
            assignee = m.group(4)
            # Title is on the next non-empty line, indented
            title = ""
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("#"):
                stripped = lines[j].strip()
                if stripped and not stripped.startswith("→"):
                    title = stripped
                    break
                j += 1
            issues.append({
                "number": number,
                "repo": repo,
                "estimate": None if pts == "—" else pts,
                "assignee": None if assignee == "unassigned" else assignee,
                "title": title,
            })
        i += 1
    return issues


# Sub-issue helpers moved to zh_graphql_ops.py — the MCP server now talks
# directly to ZenHub's GraphQL API for the sub-issue family of tools
# (list / add / remove / reorder). The bash text contract used in v1.5.0
# was a recurring source of drift; v1.6.0 retires it entirely.


def _parse_new_issue_number(plain: str) -> int | None:
    """Extract issue number from 'Created issue #NNN' output."""
    m = re.search(r"Created issue #(\d+)", plain)
    return int(m.group(1)) if m else None


def _parse_new_epic_number(plain: str) -> int | None:
    """Extract epic number from 'Created epic #NNN' output."""
    m = re.search(r"Created epic #(\d+)", plain)
    return int(m.group(1)) if m else None


# =============================================================================
# MCP server
# =============================================================================

mcp = FastMCP("zenhub")


# -----------------------------------------------------------------------------
# READ TOOLS (safe — no side effects)
# -----------------------------------------------------------------------------

@mcp.tool()
def board(repo_path: str = "") -> dict:
    """Get board overview — pipeline names and issue counts.

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.
            If omitted, uses ZH_DEFAULT_REPO_PATH env var or server cwd.

    Returns:
        dict with keys:
            ok (bool), pipelines (dict of pipeline_name → count),
            raw (the original colored stdout), stderr (any error output).
    """
    r = _run_zh(["board"], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "pipelines": _parse_board(r["stdout_plain"]) if r["ok"] else {},
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def pipeline(name: str, repo_path: str = "") -> dict:
    """List issues in a specific pipeline.

    Args:
        name: Pipeline name (e.g., "Sprint Backlog", "Product Backlog").
            Case-insensitive partial matching is supported by zh.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, pipeline (name), issues (list of {number, repo,
        estimate, assignee, title}), raw, stderr.
    """
    r = _run_zh(["pipeline", name], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "pipeline": name,
        "issues": _parse_pipeline_listing(r["stdout_plain"]) if r["ok"] else [],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def pipelines(repo_path: str = "") -> dict:
    """List all pipeline names for the workspace.

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, pipelines (list of pipeline name strings), raw, stderr.
    """
    r = _run_zh(["pipelines"], cwd=_resolve_cwd(repo_path))
    names = []
    if r["ok"]:
        for line in r["stdout_plain"].splitlines():
            stripped = line.strip()
            # Skip headers and tips
            if (stripped and not stripped.startswith("Info:")
                    and not stripped.startswith("Tip:")
                    and not stripped.startswith("Workspace:")
                    and not stripped.startswith("Pipelines:")
                    and not stripped.startswith("Use ")):
                names.append(stripped)
    return {
        "ok": r["ok"],
        "pipelines": names,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def issue(number: int, repo_path: str = "") -> dict:
    """Get full detail for a single issue.

    Args:
        number: GitHub issue number.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, raw (the full formatted issue output), stderr.
    """
    r = _run_zh(["issue", str(number)], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def mine(user: str = "", repo_path: str = "") -> dict:
    """List issues assigned to the current user (or specified user).

    Args:
        user: Optional GitHub username. Defaults to authenticated user.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, issues (parsed listing), raw, stderr.
    """
    args = ["mine"]
    if user:
        args.append(user)
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "issues": _parse_pipeline_listing(r["stdout_plain"]) if r["ok"] else [],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_list(repo_path: str = "") -> dict:
    """List all ZenHub epics in the workspace (both OPEN and CLOSED).

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epics (list of {number, state, title}), raw, stderr.
    """
    r = _run_zh(["epic", "list"], cwd=_resolve_cwd(repo_path))
    epics = []
    if r["ok"]:
        for line in r["stdout_plain"].splitlines():
            m = re.match(r"^\s*#(\d+)\s+(OPEN|CLOSED)\s+(.+?)\s*$", line)
            if m:
                epics.append({
                    "number": int(m.group(1)),
                    "state": m.group(2),
                    "title": m.group(3).strip(),
                })
    return {
        "ok": r["ok"],
        "epics": epics,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_show(epic_number: int, repo_path: str = "") -> dict:
    """Show full detail for a single epic (metadata + child issues).

    Args:
        epic_number: ZenHub epic number.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, raw (the full formatted epic output), stderr.
    """
    r = _run_zh(["epic", "show", str(epic_number)], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": epic_number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def list_users(repo_path: str = "") -> dict:
    """List users that can be assigned to issues in this workspace.

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, raw (formatted user listing), stderr.
    """
    r = _run_zh(["users"], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def list_labels(repo_path: str = "") -> dict:
    """List available labels in the workspace.

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, raw (formatted label listing), stderr.
    """
    r = _run_zh(["labels"], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def list_types(repo_path: str = "") -> dict:
    """List available issue types (Task, Feature, Bug, etc.).

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, raw (formatted type listing), stderr.
    """
    r = _run_zh(["types"], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


# -----------------------------------------------------------------------------
# SIMILARITY SEARCH TOOLS
#
# Sentence-embedding-backed search to surface tickets that look
# semantically similar to a query string (or a proposed new title+body).
# Catches paraphrased duplicates that keyword search misses.
#
# Cache lives at ~/.config/zh/index/<owner_repo>.pkl (durable across
# reboots). Implementation is in similarity.py.
# -----------------------------------------------------------------------------


def _similarity_repo(repo_path: str) -> tuple[str | None, str | None]:
    """Resolve which `owner/repo` similarity search should target.

    Returns (repo, error_message). Exactly one will be non-None.
    """
    try:
        # Lazy import: keeps the bootstrap path cheap if the tool is
        # never invoked (sentence-transformers brings in torch).
        from similarity import repo_from_cwd

        return repo_from_cwd(_resolve_cwd(repo_path)), None
    except Exception as e:
        return None, str(e)


@mcp.tool()
def zh_similar(query: str, top_k: int = 5, threshold: float = 0.5,
               repo_path: str = "") -> dict:
    """Find issues semantically similar to a query string.

    Uses sentence-transformer embeddings (all-MiniLM-L6-v2) over the
    titles + body previews of every open issue in the repo. Catches
    paraphrased duplicates that keyword search misses.

    The cache auto-refreshes on a 5-minute TTL via GitHub's
    `?since=<ISO8601>` filter — only changed issues get re-embedded.
    First call after a wipe (or first call ever) triggers a full pull
    and may take 30-60s depending on backlog size.

    Args:
        query: text to compare against existing issues. Free-form.
        top_k: max results to return (default 5).
        threshold: minimum cosine similarity (0.0-1.0) to include
            (default 0.5 — moderate matches and above).
        repo_path: Optional absolute path of a git checkout. Used to
            derive `owner/repo` for the search.

    Returns:
        dict with: ok, repo, matches (list of {number, repo, title,
        body_preview, state, similarity}), stderr.
    """
    repo, err = _similarity_repo(repo_path)
    if err:
        return {"ok": False, "matches": [], "stderr": err}
    try:
        from similarity import find_similar

        results = find_similar(
            query, repo, top_k=top_k, threshold=threshold, auto_sync=True
        )
        return {
            "ok": True,
            "repo": repo,
            "matches": [m.to_dict() for m in results],
            "stderr": "",
        }
    except Exception as e:
        return {"ok": False, "repo": repo, "matches": [], "stderr": str(e)}


@mcp.tool()
def zh_reindex(full: bool = False, repo_path: str = "") -> dict:
    """Refresh the similarity-search cache for this repo.

    Most callers don't need this — `zh_similar` auto-syncs on a
    5-minute TTL. Use this to force a refresh after a known external
    change burst, or pass `full=True` to rebuild from scratch (useful
    if the cache looks corrupted).

    Args:
        full: if True, drop the existing cache and pull every open
            issue from scratch. Otherwise do a delta sync from the
            cache's last indexed_at timestamp.
        repo_path: Optional absolute path of a git checkout to derive
            owner/repo from.

    Returns:
        dict with: ok, repo, mode ('full'/'delta'/'skipped'),
        added, updated, removed, indexed_at, total_entries, stderr.
    """
    repo, err = _similarity_repo(repo_path)
    if err:
        return {"ok": False, "stderr": err}
    try:
        from similarity import reindex

        result = reindex(repo, full=full)
        result["stderr"] = ""
        return result
    except Exception as e:
        return {"ok": False, "repo": repo, "stderr": str(e)}


# -----------------------------------------------------------------------------
# WRITE TOOLS — ISSUE LIFECYCLE
# -----------------------------------------------------------------------------

@mcp.tool()
def create_issue(title: str, body: str, type: str = "Task",
                 pipeline: str = "Product Backlog",
                 labels: str = "", repo_path: str = "",
                 confirm_create: bool = False,
                 skip_duplicate_check: bool = False) -> dict:
    """Create a new ZenHub issue.

    Runs a pre-flight similarity check against existing open issues
    (via the sentence-embedding index — see `zh_similar`). When a
    near-duplicate is detected (cosine similarity above the hard
    threshold), the create is BLOCKED and the candidate matches are
    returned — pass `confirm_create=True` to override and create
    anyway. Soft matches are surfaced as a warning but don't block.

    Args:
        title: Issue title (required, non-empty).
        body: Issue body / description in Markdown (required, non-empty).
        type: Issue type (Task / Feature / Bug / Spike / Research / Sub-task).
            Defaults to Task.
        pipeline: Target pipeline. Defaults to "Product Backlog".
        labels: Comma-separated label names (optional).
        repo_path: Optional absolute path of a git checkout to run zh from.
        confirm_create: pass True to bypass the duplicate-check block.
            Use ONLY after reviewing the returned matches and confirming
            the new ticket is genuinely distinct.
        skip_duplicate_check: pass True to skip the pre-flight entirely
            (e.g. when migrating issues in bulk or when the similarity
            index is known to be unavailable). Prefer `confirm_create`
            for one-off overrides.

    Returns:
        On block: dict with ok=False, blocked=True, duplicate_check (the
            candidate matches and recommendation), and a clear message
            explaining how to override.
        On success: dict with ok=True, number (new issue number), url,
            raw, stderr, duplicate_check (informational — may include
            soft matches).
    """
    if not title.strip():
        return {"ok": False, "stderr": "title must be non-empty"}
    if not body.strip():
        return {"ok": False, "stderr": "body must be non-empty"}

    # Pre-flight similarity check
    dup_info = None
    if not skip_duplicate_check:
        repo, err = _similarity_repo(repo_path)
        if err:
            # Can't derive repo → log but don't fail the create.
            dup_info = {"ok": False, "stderr": err, "matches": []}
        else:
            try:
                from similarity import check_duplicate

                dup_info = check_duplicate(title, body, repo)
            except Exception as e:
                # Embedding failure shouldn't block create — log only.
                dup_info = {
                    "ok": False,
                    "stderr": f"duplicate check failed: {e}",
                    "matches": [],
                }

        if (dup_info and dup_info.get("recommendation") == "block"
                and not confirm_create):
            return {
                "ok": False,
                "blocked": True,
                "stderr": (
                    "Refused: a similar open issue already exists "
                    "(cosine similarity >= "
                    f"{dup_info.get('hard_threshold')}). "
                    "Review duplicate_check.matches; if the new ticket is "
                    "genuinely distinct, retry with confirm_create=True."
                ),
                "duplicate_check": dup_info,
            }

    args = ["create", title, "-t", type, "-p", pipeline, "-b", body]
    if labels:
        args.extend(["-l", labels])
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    out = {
        "ok": r["ok"],
        "number": _parse_new_issue_number(r["stdout_plain"]) if r["ok"] else None,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }
    if dup_info is not None:
        out["duplicate_check"] = dup_info
    return out


@mcp.tool()
def close_issue(number: int, comment: str = "", repo_path: str = "") -> dict:
    """Close an issue (moves to Closed pipeline) with an optional closing comment.

    DESTRUCTIVE — sends notifications to issue watchers. Pre-confirm before
    invoking on tickets you don't own.

    Args:
        number: Issue number to close.
        comment: Optional closing comment (recommended — explain WHY).
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, raw, stderr.
    """
    args = ["close", str(number)]
    if comment:
        args.append(comment)
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def reopen_issue(number: int, repo_path: str = "") -> dict:
    """Reopen a closed issue.

    Args:
        number: Issue number to reopen.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, raw, stderr.
    """
    r = _run_zh(["reopen", str(number)], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def move_issue(number: int, pipeline: str, repo_path: str = "") -> dict:
    """Move an issue to a different pipeline.

    Args:
        number: Issue number.
        pipeline: Destination pipeline name (case-insensitive partial match).
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, target_pipeline, raw, stderr.
    """
    r = _run_zh(["move", str(number), pipeline], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "target_pipeline": pipeline,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def reorder_issue(number: int, position: str, repo_path: str = "") -> dict:
    """Reorder an issue within its current pipeline.

    Args:
        number: Issue number.
        position: Either a numeric string (e.g., "5"), "top", or "bottom".
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, position, raw, stderr.
    """
    r = _run_zh(["reorder", str(number), position],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "position": position,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def comment(number: int, message: str, repo_path: str = "") -> dict:
    """Add a comment to an issue.

    Args:
        number: Issue number.
        message: Comment text (Markdown supported).
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, raw, stderr.
    """
    if not message.strip():
        return {"ok": False, "stderr": "message must be non-empty"}
    r = _run_zh(["comment", str(number), "-m", message],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def assign(number: int, user: str, repo_path: str = "") -> dict:
    """Assign a user to an issue.

    Args:
        number: Issue number.
        user: GitHub username to assign.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, user, raw, stderr.
    """
    r = _run_zh(["assign", str(number), user], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "user": user,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def unassign(number: int, user: str = "", repo_path: str = "") -> dict:
    """Remove assignee(s) from an issue.

    Args:
        number: Issue number.
        user: Optional specific user to unassign. If omitted, removes all assignees.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, raw, stderr.
    """
    args = ["unassign", str(number)]
    if user:
        args.append(user)
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def set_estimate(number: int, points: str, repo_path: str = "") -> dict:
    """Set or clear an issue's story-point estimate.

    Args:
        number: Issue number.
        points: Numeric estimate (e.g., "3") or "clear" to remove.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, points, raw, stderr.
    """
    r = _run_zh(["estimate", str(number), points],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "points": points,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def set_priority(number: int, level: str, repo_path: str = "") -> dict:
    """Set or clear an issue's priority.

    Args:
        number: Issue number.
        level: One of "high", "medium", "low", "clear".
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, number, level, raw, stderr.
    """
    r = _run_zh(["priority", str(number), level],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "level": level,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


# -----------------------------------------------------------------------------
# WRITE TOOLS — DEPENDENCIES
# -----------------------------------------------------------------------------

@mcp.tool()
def block_issue(blocked: int, blocking: int, repo_path: str = "") -> dict:
    """Set issue dependency: `blocked` is blocked BY `blocking`.

    Args:
        blocked: Issue that is blocked.
        blocking: Issue that is blocking the other.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, blocked, blocking, raw, stderr.
    """
    r = _run_zh(["block", str(blocked), str(blocking)],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "blocked": blocked,
        "blocking": blocking,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


# -----------------------------------------------------------------------------
# WRITE TOOLS — EPIC MANAGEMENT
# -----------------------------------------------------------------------------

@mcp.tool()
def epic_create(title: str, description: str = "", labels: str = "",
                repo_path: str = "") -> dict:
    """Create a new ZenHub epic.

    Args:
        title: Epic title (required, non-empty).
        description: Optional epic body / description.
        labels: Optional comma-separated label names.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, raw, stderr.
    """
    if not title.strip():
        return {"ok": False, "stderr": "title must be non-empty"}
    args = ["epic", "create", title]
    if description:
        args.extend(["-d", description])
    if labels:
        args.extend(["-l", labels])
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": _parse_new_epic_number(r["stdout_plain"]) if r["ok"] else None,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_update(epic_number: int, title: str = "", description: str = "",
                repo_path: str = "") -> dict:
    """Update an epic's title and/or description.

    At least one of `title` or `description` must be provided.

    Args:
        epic_number: ZenHub epic number.
        title: New title (optional).
        description: New body / description (optional).
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, raw, stderr.
    """
    if not title and not description:
        return {
            "ok": False,
            "stderr": "Must provide title and/or description",
        }
    args = ["epic", "update", str(epic_number)]
    if title:
        args.extend(["-t", title])
    if description:
        args.extend(["-d", description])
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": epic_number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_add_children(epic_number: int, issue_numbers: list[int],
                      repo_path: str = "") -> dict:
    """Add one or more issues to an epic.

    Args:
        epic_number: ZenHub epic number.
        issue_numbers: List of issue numbers to add (single API call).
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, added (list of issue numbers), raw, stderr.
    """
    if not issue_numbers:
        return {"ok": False, "stderr": "issue_numbers must be non-empty"}
    args = ["epic", "add", str(epic_number)] + [str(n) for n in issue_numbers]
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": epic_number,
        "added": issue_numbers if r["ok"] else [],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_remove_children(epic_number: int, issue_numbers: list[int],
                         repo_path: str = "") -> dict:
    """Remove one or more issues from an epic.

    Args:
        epic_number: ZenHub epic number.
        issue_numbers: List of issue numbers to remove.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, removed (list of issue numbers), raw, stderr.
    """
    if not issue_numbers:
        return {"ok": False, "stderr": "issue_numbers must be non-empty"}
    args = ["epic", "remove", str(epic_number)] + [str(n) for n in issue_numbers]
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": epic_number,
        "removed": issue_numbers if r["ok"] else [],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_close(epic_number: int, repo_path: str = "") -> dict:
    """Close an epic.

    DESTRUCTIVE — affects board visibility. Pre-confirm.

    Args:
        epic_number: ZenHub epic number.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, raw, stderr.
    """
    r = _run_zh(["epic", "close", str(epic_number)],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": epic_number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_reopen(epic_number: int, repo_path: str = "") -> dict:
    """Reopen a closed epic.

    Args:
        epic_number: ZenHub epic number.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, raw, stderr.
    """
    r = _run_zh(["epic", "reopen", str(epic_number)],
                cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "epic_number": epic_number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


# Note: epic_delete deliberately not exposed as an MCP tool. Permanently
# deleting an epic is irreversible. If you genuinely need to delete an epic,
# do it via `zh epic delete <N>` directly from the CLI — that requires
# human deliberation.


# =============================================================================
# Sub-issue management (Issue → Sub-issue hierarchy tier)
#
# v1.6.0: these tools call ZenHub's GraphQL API directly via zh_graphql_ops.
# No more text-contract parsing — the layer that drove four rounds of
# release-review findings on v1.5.0 is gone.
# =============================================================================


def _resolve_ctx(repo_path: str = ""):
    """Resolve a RepoContext from the cwd; return (ctx, error_dict).

    Exactly one of (ctx, error_dict) is non-None.
    """
    from zh_api import resolve_context, ZhApiError  # noqa: PLC0415
    try:
        return resolve_context(cwd=_resolve_cwd(repo_path)), None
    except ZhApiError as e:
        return None, {"ok": False, "stderr": str(e)}
    except Exception as e:  # noqa: BLE001 — be loud about unexpected
        return None, {"ok": False, "stderr": f"context resolution failed: {e}"}


@mcp.tool()
def subissue_list(parent_number: int, repo_path: str = "") -> dict:
    """List sub-issues of a parent issue.

    Calls ZenHub's GraphQL `zenhubChildIssues` connection directly from
    Python — no bash text contract. Walks pagination with the
    stuck-cursor + iteration-cap defenses carried over from the bash
    implementation. Each child dict carries its `repository.owner` and
    `.name` so callers can spot cross-repo children that can't be
    operated on from a single git checkout.

    Args:
        parent_number: Issue number of the parent (positive int).
        repo_path: Optional absolute path of a git checkout to run from.
            Used to derive owner/repo via `git remote get-url origin`.

    Returns:
        dict with:
            ok: bool
            parent_number: int
            parent_title: str
            parent_state: str | None — "OPEN" / "CLOSED"
            total_count: int — API's zenhubChildIssues.totalCount
            fetched_count: int — how many CHILD nodes we walked
            children: list[dict] each with
                number: int
                title: str (untruncated, separator-safe)
                state: "OPEN" | "CLOSED"
                pipeline: str | None
                assignees: list[str]
                repository: {"owner": str, "name": str}
            pagination_warning: str | None — set if the walk bailed
                defensively (cursor stuck, iteration cap reached)
            stderr: str
    """
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        # Review finding #8: `parent_state` was missing from the early-
        # error return shape while the docstring + other returns all
        # include it. Callers reading `result["parent_state"]` would
        # KeyError when context resolution fails (no git remote, etc.).
        return {**err, "parent_number": parent_number,
                "parent_title": "", "parent_state": None,
                "total_count": 0, "fetched_count": 0,
                "children": [], "pagination_warning": None}
    from zh_graphql_ops import list_sub_issues  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = list_sub_issues(ctx, parent_number)
    except ZhApiError as e:
        return {
            "ok": False,
            "parent_number": parent_number,
            "parent_title": "",
            "parent_state": None,
            "total_count": 0,
            "fetched_count": 0,
            "children": [],
            "pagination_warning": None,
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "parent_number": parent_number,
        "parent_title": result.get("parent_title", ""),
        "parent_state": result.get("parent_state"),
        "total_count": result.get("total_count", 0),
        "fetched_count": result.get("fetched_count", 0),
        "children": result.get("children", []),
        "pagination_warning": result.get("pagination_warning"),
        "stderr": result.get("error") or "",
    }


@mcp.tool()
def subissue_add_children(parent_number: int, child_numbers: list[int],
                          repo_path: str = "") -> dict:
    """Add one or more issues as sub-issues of a parent.

    Calls ZenHub's `addSubIssues` mutation directly. The API's
    `replaceParent` defaults to false in our caller, so a child that's
    already attached to a different parent is surfaced in the API's
    `failedIssues` array — NOT silently re-parented. The MCP wrapper
    reports succeeded / failed sets sourced from that response, not from
    the raw input list (a contract finding the v1.5.0 series ate hard).

    `outcome="noop"` is the (success=0, failed=0) case — the API neither
    added nor rejected anything, typically because every requested child
    was already linked to this parent. `ok` is false so an LLM caller
    cannot confuse it with a successful add.

    Args:
        parent_number: Issue number of the parent.
        child_numbers: List of issue numbers to link (single API call).
        repo_path: Optional absolute path of a git checkout.

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            parent_number: int
            outcome: "ok" | "partial" | "fail" | "noop"
            success_count: int — API-reported successCount
            failed_count: int — API-reported failedIssues length
            succeeded: list[int] — children the API actually linked
            failed: list[dict] — each {number, owner, name}
            github_errors: dict | None
            stderr: str
    """
    if not child_numbers:
        # Full result shape on the empty-input guard so strict MCP
        # callers don't KeyError on documented keys after a guard
        # rejection. Mirrors the sprint-tool fix from `bef3313`.
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": "child_numbers must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "parent_number": parent_number, "outcome": "fail",
                "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [], "github_errors": None,
                "partial_success_warning": None}
    from zh_graphql_ops import add_sub_issues  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = add_sub_issues(ctx, parent_number, list(child_numbers))
    except ZhApiError as e:
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "parent_number": parent_number,
        "outcome": result.get("outcome", "fail"),
        "success_count": result.get("success_count", 0),
        "failed_count": result.get("failed_count", 0),
        "succeeded": result.get("succeeded", []),
        "failed": result.get("failed", []),
        "github_errors": result.get("github_errors"),
        "partial_success_warning": result.get("partial_success_warning"),
        "stderr": result.get("error") or "",
    }


@mcp.tool()
def subissue_remove_children(parent_number: int, child_numbers: list[int],
                             repo_path: str = "") -> dict:
    """Remove one or more sub-issues from a parent.

    Pre-validates that every child currently has `parent_number` as its
    parent and lives in the cwd's repo; on failure surfaces a
    consolidated mismatch report rather than bailing at the first error.

    `outcome="noop"` (success=0, failed=0 from the API after pre-flight
    validation passed) is reported with `ok=False` — that combination is
    an API-side oddity (e.g. a race where someone else unlinked between
    pre-flight and mutation) and shouldn't look like success.

    Args:
        parent_number: Issue number of the parent.
        child_numbers: List of sub-issue numbers to unlink.
        repo_path: Optional absolute path of a git checkout.

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            parent_number: int
            outcome: "ok" | "partial" | "fail" | "noop"
            success_count: int
            failed_count: int
            succeeded: list[int]
            failed: list[dict]
            github_errors: dict | None
            stderr: str
    """
    if not child_numbers:
        # Full result shape on the empty-input guard so strict MCP
        # callers don't KeyError on documented keys after a guard
        # rejection. Mirrors the sprint-tool fix from `bef3313`.
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": "child_numbers must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "parent_number": parent_number, "outcome": "fail",
                "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [], "github_errors": None,
                "partial_success_warning": None}
    from zh_graphql_ops import remove_sub_issues  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = remove_sub_issues(ctx, parent_number, list(child_numbers))
    except ZhApiError as e:
        return {
            "ok": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "parent_number": parent_number,
        "outcome": result.get("outcome", "fail"),
        "success_count": result.get("success_count", 0),
        "failed_count": result.get("failed_count", 0),
        "succeeded": result.get("succeeded", []),
        "failed": result.get("failed", []),
        "github_errors": result.get("github_errors"),
        "partial_success_warning": result.get("partial_success_warning"),
        "stderr": result.get("error") or "",
    }


@mcp.tool()
def subissue_reorder(child_number: int, position: str,
                     sibling_number: int | None = None,
                     repo_path: str = "") -> dict:
    """Reorder a sub-issue among its siblings.

    Calls ZenHub's `reprioritizeSubIssue` mutation directly. Sibling
    anchoring (top/bottom/after/before) is computed in Python from
    `zenhubChildIssues` listing — same logic the bash implementation had,
    just on the MCP side now.

    Positions:
      - "top" / "first"   — first sibling
      - "bottom" / "last" — last sibling
      - "after"           — requires sibling_number
      - "before"          — requires sibling_number

    `outcome="noop"` is the only-child case (no sibling to anchor
    against); the mutation is NOT fired. `ok=False` in that case so the
    caller cannot confuse it with a successful reorder.

    Args:
        child_number: Sub-issue to reposition.
        position: One of "top" / "first" / "bottom" / "last" /
            "after" / "before".
        sibling_number: Required when position is after/before.
        repo_path: Optional absolute path of a git checkout.

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            child_number: int
            parent_number: int | None — resolved from the child's
                parentIssue
            position: str — normalized human form, e.g. "top",
                "after #101"
            outcome: "ok" | "noop" | "fail"
            stderr: str
    """
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "child_number": child_number,
                "parent_number": None, "position": position,
                "outcome": "fail"}
    from zh_graphql_ops import reorder_sub_issue  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = reorder_sub_issue(
            ctx, child_number, position,
            sibling_number=sibling_number,
        )
    except ZhApiError as e:
        return {
            "ok": False,
            "child_number": child_number,
            "parent_number": None,
            "position": position,
            "outcome": "fail",
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "child_number": child_number,
        "parent_number": result.get("parent_number"),
        "position": result.get("position", position),
        "outcome": result.get("outcome", "fail"),
        "stderr": result.get("error") or "",
    }


# =============================================================================
# Sprint tools (v1.6.0)
#
# Sprint functionality inspired by the design proposed in PR #2 by
# @jeremiahrose; ported here against the new direct-GraphQL pattern.
# =============================================================================


@mcp.tool()
def sprint_list(repo_path: str = "", include_closed: bool = False) -> dict:
    """List sprints in the workspace.

    Args:
        repo_path: Optional absolute path of a git checkout to derive
            owner/repo from.
        include_closed: include CLOSED sprints in the listing. Defaults
            to OPEN-only.

    Returns:
        dict with:
            ok: bool
            workspace_name: str
            active_sprint_id: str | None
            sprints: list[dict] — each with
                id: str
                name: str
                state: "OPEN" | "CLOSED"
                start_at: ISO8601 datetime string | None
                end_at: ISO8601 datetime string | None
                completed_points: float
                total_points: float
                closed_issues_count: int
                is_active: bool
            stderr: str
    """
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "workspace_name": "", "active_sprint_id": None,
                "sprints": [], "pagination_warning": None}
    from zh_graphql_ops import list_sprints  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = list_sprints(ctx, include_closed=include_closed)
    except ZhApiError as e:
        return {
            "ok": False,
            "workspace_name": "",
            "active_sprint_id": None,
            "sprints": [],
            "pagination_warning": None,
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "workspace_name": result.get("workspace_name", ""),
        "active_sprint_id": result.get("active_sprint_id"),
        "sprints": result.get("sprints", []),
        "pagination_warning": result.get("pagination_warning"),
        "stderr": "",
    }


@mcp.tool()
def sprint_show(sprint_name: str, repo_path: str = "") -> dict:
    """Get full detail + issues for a sprint named `sprint_name`.

    `sprint_name` accepts "current" or "active" as aliases for the
    workspace's active sprint. Exact matches are case-insensitive.

    Args:
        sprint_name: Sprint name (or "current"/"active").
        repo_path: Optional absolute path of a git checkout.

    Returns:
        dict with:
            ok: bool
            sprint_id: str | None
            sprint_name: str
            state: "OPEN" | "CLOSED" | None
            start_at: ISO8601 string | None
            end_at: ISO8601 string | None
            completed_points: float
            total_points: float
            closed_issues_count: int
            description: str | None
            issue_count: int
            issues: list[dict] — each with
                number: int
                title: str
                state: "OPEN" | "CLOSED"
                html_url: str
                estimate: number | None
                assignees: list[str]
                pipeline: str | None
                repository: {"owner": str, "name": str}
            stderr: str
    """
    if not sprint_name or not str(sprint_name).strip():
        # Full result shape with stderr — strict MCP callers shouldn't
        # KeyError on documented keys after the guard. Review #9.
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "state": None,
            "start_at": None,
            "end_at": None,
            "completed_points": 0.0,
            "total_points": 0.0,
            "closed_issues_count": 0,
            "description": None,
            "issue_count": 0,
            "issues": [],
            "pagination_warning": None,
            "stderr": "sprint_name must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "sprint_id": None, "sprint_name": sprint_name,
                "state": None, "start_at": None, "end_at": None,
                "completed_points": 0.0, "total_points": 0.0,
                "closed_issues_count": 0, "description": None,
                "issue_count": 0, "issues": [],
                "pagination_warning": None}
    from zh_graphql_ops import get_sprint_detail  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = get_sprint_detail(ctx, sprint_name)
    except ZhApiError as e:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "state": None,
            "start_at": None,
            "end_at": None,
            "completed_points": 0.0,
            "total_points": 0.0,
            "closed_issues_count": 0,
            "description": None,
            "issue_count": 0,
            "issues": [],
            "pagination_warning": None,
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "sprint_id": result.get("sprint_id"),
        "sprint_name": result.get("sprint_name", sprint_name),
        "state": result.get("state"),
        "start_at": result.get("start_at"),
        "end_at": result.get("end_at"),
        "completed_points": result.get("completed_points", 0.0),
        "total_points": result.get("total_points", 0.0),
        "closed_issues_count": result.get("closed_issues_count", 0),
        "description": result.get("description"),
        "issue_count": result.get("issue_count", 0),
        "issues": result.get("issues", []),
        "pagination_warning": result.get("pagination_warning"),
        "stderr": result.get("error") or "",
    }


@mcp.tool()
def sprint_current(repo_path: str = "") -> dict:
    """Get full detail + issues for the workspace's active sprint.

    Convenience wrapper for `sprint_show("current")`. Returns the same
    shape; `ok=False` with a clear `stderr` if no active sprint exists.
    """
    return sprint_show("current", repo_path=repo_path)


@mcp.tool()
def sprint_add_issues(sprint_name: str, issue_numbers: list[int],
                      repo_path: str = "") -> dict:
    """Add one or more issues to a sprint.

    Partial-failure handling mirrors the sub-issue family: the API's
    `addIssuesToSprints` returns the list of SprintIssue links it
    actually created. Issues absent from that list are inferred-failed
    (the GraphQL surface doesn't say WHY a link didn't form — usually
    the issue was already in the sprint, archived, or otherwise
    ineligible). `succeeded` / `failed` are split from the response,
    never from the raw input.

    Args:
        sprint_name: Sprint name to target. `current` / `active` are
            aliases for the workspace's active sprint.
        issue_numbers: List of issue numbers in the cwd's repo.
        repo_path: Optional git checkout for repo + workspace context.

    Returns:
        dict with:
            ok: bool — true iff outcome == "ok"
            sprint_id: str | None
            sprint_name: str
            outcome: "ok" | "partial" | "fail" | "noop"
            success_count: int
            failed_count: int
            succeeded: list[int] — API confirmed these were linked
            failed: list[int] — API did not return links for these
            stderr: str
    """
    # Full result shape on the empty-input guards so strict MCP callers
    # don't KeyError on documented keys after a guard rejection.
    # Review #9.
    if not issue_numbers:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "stderr": "issue_numbers must be non-empty",
        }
    if not sprint_name or not str(sprint_name).strip():
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "stderr": "sprint_name must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "sprint_id": None, "sprint_name": sprint_name,
                "outcome": "fail", "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": []}
    from zh_graphql_ops import add_issues_to_sprint  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = add_issues_to_sprint(ctx, sprint_name, list(issue_numbers))
    except ZhApiError as e:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "sprint_id": result.get("sprint_id"),
        "sprint_name": result.get("sprint_name", sprint_name),
        "outcome": result.get("outcome", "fail"),
        "success_count": result.get("success_count", 0),
        "failed_count": result.get("failed_count", 0),
        "succeeded": result.get("succeeded", []),
        "failed": result.get("failed", []),
        "stderr": result.get("error") or "",
    }


@mcp.tool()
def sprint_remove_issues(sprint_name: str, issue_numbers: list[int],
                         repo_path: str = "") -> dict:
    """Remove one or more issues from a sprint.

    Partial-failure handling: the API returns the sprint's post-
    mutation state. We compare the input numbers against the sprint's
    post-state `sprintIssues`; anything STILL attached after the
    mutation is inferred-failed. For sprints with >100 issues, OR
    when the mutation response omits the target sprint entirely, we
    walk the sprint directly to determine the authoritative post-
    state. Each post-state issue is filtered by repository so a
    sibling repo's same-numbered issue can't mis-classify our
    removal.

    Returns the same shape as `sprint_add_issues`, plus:
      - `inspected_full`: bool — True when we walked every page (or
        the response was complete on its own).
      - `pagination_warning`: str | None — surfaced when the follow-
        up walk bailed defensively (stuck cursor / iteration cap).
      - `response_anomaly`: str | None — surfaced when the mutation
        response omitted or returned an empty `sprints` array.
    """
    if not issue_numbers:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "stderr": "issue_numbers must be non-empty",
        }
    if not sprint_name or not str(sprint_name).strip():
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "stderr": "sprint_name must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "sprint_id": None, "sprint_name": sprint_name,
                "outcome": "fail", "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [],
                "inspected_full": False,
                "pagination_warning": None,
                "response_anomaly": None}
    from zh_graphql_ops import remove_issues_from_sprint  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = remove_issues_from_sprint(
            ctx, sprint_name, list(issue_numbers)
        )
    except ZhApiError as e:
        return {
            "ok": False,
            "sprint_id": None,
            "sprint_name": sprint_name,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "stderr": str(e),
        }
    return {
        "ok": result.get("ok", False),
        "sprint_id": result.get("sprint_id"),
        "sprint_name": result.get("sprint_name", sprint_name),
        "outcome": result.get("outcome", "fail"),
        "success_count": result.get("success_count", 0),
        "failed_count": result.get("failed_count", 0),
        "succeeded": result.get("succeeded", []),
        "failed": result.get("failed", []),
        "inspected_full": result.get("inspected_full", False),
        "pagination_warning": result.get("pagination_warning"),
        "response_anomaly": result.get("response_anomaly"),
        "stderr": result.get("error") or "",
    }


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    mcp.run()
