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

v1.9.0 model migration: ZenHub removed Legacy Epics and ZenhubEpics in June
2025. The epic_* tools no longer hit the dead ZenhubEpic API; an epic is now a
normal issue whose issue-type is Epic, with children wired via Sub-Issues. The
same machinery backs first-class tools for every planning level (initiative /
project / epic / subtask) plus set_issue_type and list_priorities. Type
discovery uses assignableIssueTypes (the full 5-level hierarchy), not the old
githubIssueTypes repo query.

Every tool optionally accepts a `repo_path` argument — the absolute path of a
git checkout that the underlying `zh` invocation runs from. This is required
because `zh` detects the GitHub repo via `git config --get remote.origin.url`
from its working directory. If omitted, falls back to:
  1. ZH_DEFAULT_REPO_PATH environment variable
  2. The MCP server's current working directory at launch time

Run as a subprocess (stdio transport):
    /usr/bin/python3 mcp_server.py

The script self-bootstraps a durable venv under XDG_DATA_HOME (default
`~/.local/share/zh/venv`) on first run, validates it on every launch, and
re-execs under that venv. Any python3 on PATH that can run `python3 -m venv`
works as the launcher.

Register user-scope so every Claude Code session sees it:
    claude mcp add --scope user zenhub \\
        /usr/bin/python3 \\
        /path/to/zenhub-cli/mcp_server.py

Environment overrides:
  ZH_DEFAULT_REPO_PATH — default git-checkout dir to run zh from
                         (otherwise uses MCP server cwd at launch)
  ZH_BIN_PATH          — path to zh bash script (default: peer to this file)
  ZH_MCP_VENV          — full ABSOLUTE path of the venv directory to use;
                         overrides the XDG_DATA_HOME-derived default. Useful
                         for pinning to a project-local venv during
                         development. Relative paths are rejected.
  XDG_DATA_HOME        — standard XDG override for the data root; the venv
                         is created at `$XDG_DATA_HOME/zh/venv`.
  ZH_MCP_PROBE_TIMEOUT — seconds for the per-launch `import` probe that
                         validates the venv (default 30). Widen on slow
                         media (NFS home, FileVault cold cache) where the
                         import can otherwise time out and trigger a
                         needless rebuild.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

# -----------------------------------------------------------------------------
# Self-bootstrap: build (or rebuild, if broken / stale) a durable venv under
# XDG_DATA_HOME, validate it, and re-exec under it. Must run before any
# third-party import (mcp).
# -----------------------------------------------------------------------------


def _default_venv_dir() -> tuple[Path, bool]:
    """Compute the default venv location.

    Returns `(venv_dir, user_supplied)`. `user_supplied` is True when
    the path came from the explicit `ZH_MCP_VENV` env var (subject to
    strict typo protection downstream) and False when the path is the
    server-chosen XDG default (subject to permissive auto-mkdir).
    Threading the flag through to downstream callers (rather than
    re-reading the env in each consumer) keeps the classification
    coupled to the path it produced — env mutation between calls
    can't defeat the typo guard.

    Priority:
      1. ZH_MCP_VENV environment variable (full path) → user_supplied=True
      2. $XDG_DATA_HOME/zh/venv (standard XDG; defaults to
         ~/.local/share/zh/venv when XDG_DATA_HOME is unset) →
         user_supplied=False
    """
    raw_override = os.environ.get("ZH_MCP_VENV")
    if raw_override is not None and not raw_override.strip():
        # Distinguish unset from set-to-empty so a CI/Docker config that
        # did `ENV ZH_MCP_VENV=` (clearing an inherited value) or
        # `ZH_MCP_VENV="$UNSET_VAR"` doesn't silently fall through to
        # XDG. Surface the situation; still fall through so the launch
        # can succeed.
        print(
            "[zenhub-mcp] warning: ZH_MCP_VENV is set but empty / "
            "whitespace; ignoring and using XDG_DATA_HOME default.",
            file=sys.stderr,
            flush=True,
        )
    override = (raw_override or "").strip()
    if override:
        expanded = Path(override).expanduser()
        if str(expanded).startswith("~"):
            # HOME unset → expanduser leaves `~` unchanged → resolve()
            # would pin it to cwd, producing a literal `~` directory +
            # rebuild loop across cwds.
            raise RuntimeError(
                f"[zenhub-mcp] cannot resolve home directory in "
                f"ZH_MCP_VENV={override!r} (HOME is unset). Set HOME or "
                f"give ZH_MCP_VENV an absolute path."
            )
        if not expanded.is_absolute():
            # A relative ZH_MCP_VENV (e.g. `./venv` or `venv`) would be
            # `.resolve()`-d against whatever cwd the MCP launched from.
            # Claude Code launches the server from different project
            # repo_paths, so a relative path produces a DIFFERENT venv
            # per project — orphaned ~500MB venvs scattered across
            # trees. Require an absolute (or `~`-prefixed) path.
            raise RuntimeError(
                f"[zenhub-mcp] ZH_MCP_VENV must be an absolute path "
                f"(or start with `~`); got {override!r}. A relative "
                f"path would resolve differently per launch cwd."
            )
        return expanded.resolve(), True
    xdg_raw = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    xdg_data = os.path.expanduser(xdg_raw)
    if xdg_data.startswith("~"):
        # `expanduser` leaves `~` unchanged only when it can resolve
        # neither HOME nor a passwd entry for the uid — i.e. HOME is
        # unset AND `pwd.getpwuid(os.getuid())` has no home dir
        # (distroless / scratch containers, `nobody`, some sandboxes).
        # In that case `Path("~/...").resolve()` would pin against cwd
        # and produce a literal `~` directory, different across
        # launches → rebuild loops. Raise instead.
        raise RuntimeError(
            f"[zenhub-mcp] cannot resolve home directory in {xdg_raw!r} "
            f"(HOME is unset). Set HOME, set XDG_DATA_HOME to an "
            f"absolute path, or set ZH_MCP_VENV to point at the venv "
            f"location explicitly."
        )
    # `.resolve()` on both branches — pins relative paths (e.g. a
    # mis-configured `XDG_DATA_HOME=./local-data`) at startup so the
    # MCP doesn't thrash between projects when it launches from
    # different cwds, and produces consistent paths in error messages
    # regardless of which branch was taken.
    return (Path(xdg_data) / "zh" / "venv").resolve(), False


# INVARIANT: keep the LIGHTEST dependency first. `_venv_per_launch_probe`
# imports `_VENV_DEPS[0]` on every MCP launch to validate the venv, so
# the first entry must be cheap to import. Reordering this so
# `sentence-transformers` (→ torch, multi-second cold import) lands at
# [0] would make every launch slow without tripping any error — exactly
# the cost the per-launch/full probe split exists to avoid.
_VENV_DEPS = (
    "mcp",
    # similarity search: sentence-transformers brings in torch + transformers
    # + huggingface_hub. The model weights themselves are cached under
    # ~/.cache/huggingface/ so they survive even if the venv is rebuilt.
    "sentence-transformers",
    "numpy",
)
_VENV_MIN_PY = (3, 10)  # mcp package requires >= 3.10
_VENV_MARKER = ".zh-deps-hash"  # records the _VENV_DEPS hash this venv was built for
_BUILD_SENTINEL = ".zh-build-in-progress"  # written during _build_venv; absent on success
_BOOTSTRAP_LOCK = ".zh-bootstrap.lock"     # fcntl.flock file for concurrent-launch serialization

def _probe_timeout_default() -> int:
    """Per-launch probe timeout, overridable via ZH_MCP_PROBE_TIMEOUT.

    Defaults to 30s. The probe (`import mcp` and its ~30 transitive
    modules — pydantic, httpx, anyio, sse-starlette, …) can exceed a
    tight bound on a cold disk (FileVault sleep/wake, NFS-mounted home,
    Docker bind mount, AV-scanning laptop, Spotlight first read), and a
    timeout there triggers a needless multi-minute rebuild. 30s is a
    safer floor than the original 15s; the env var lets operators on
    slow media widen it further.
    """
    raw = os.environ.get("ZH_MCP_PROBE_TIMEOUT", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 30


# Subprocess timeouts. The per-launch probe stays snappy (one import) so
# slow cold-disk transformers imports don't trigger needless rebuild
# loops; the post-build probe runs once when caches are warm anyway.
# Captured once at import. The env override (ZH_MCP_PROBE_TIMEOUT) must
# therefore be set BEFORE the process starts — it won't pick up a
# mid-process change. That's correct for a real MCP server (env comes
# from the launch config), and the unit tests call _probe_timeout_default()
# directly so they're unaffected.
_VENV_PER_LAUNCH_PROBE_TIMEOUT = _probe_timeout_default()
_VENV_FULL_PROBE_TIMEOUT = 60         # all _VENV_DEPS — cold-cache torch import can take ~30s
_VENV_BUILD_TIMEOUT = 60              # `python -m venv ...`
_VENV_PIP_TIMEOUT = 600               # `pip install ...` (torch is ~400MB)


def _venv_per_launch_probe() -> str:
    """Lightweight probe — imports just the first declared dep (`mcp`) —
    runs on every MCP launch.

    Importing the full _VENV_DEPS tuple (sentence_transformers + torch +
    numpy) takes seconds on a warm cache and tens of seconds on a cold
    one — far too costly to pay on every launch. The post-build probe
    (`_venv_full_probe`) catches half-installed venvs once; per-launch
    just confirms the interpreter still runs and the primary dep
    imports. Derived from `_VENV_DEPS[0]` (not hardcoded) so a future
    rename/reorder of the deps tuple can't leave this probing a module
    that no longer exists.
    """
    return f"import {_VENV_DEPS[0].replace('-', '_')}"


def _venv_full_probe() -> str:
    """Heavyweight probe — every declared dep — runs once after build.

    Catches a partial install where one wheel landed cleanly and
    another failed mid-stream. Recomputes from `_VENV_DEPS` at call
    time so monkeypatching tests see matching imports. Maps PyPI
    names to module names by `s/-/_/`. If a future dep has a
    non-trivial mapping (`Pillow` → `PIL`), the probe fails loudly
    at the call site — desired behavior.
    """
    return "; ".join(
        f"import {name.replace('-', '_')}" for name in _VENV_DEPS
    )


def _deps_hash() -> str:
    """Stable hash of _VENV_DEPS — used to detect dep changes between launches.

    A 16-char SHA-256 prefix is plenty: collisions don't matter for a
    cache-invalidation token that's only compared against itself.
    """
    return hashlib.sha256("|".join(_VENV_DEPS).encode()).hexdigest()[:16]


def _venv_is_valid(venv_dir: Path, deps_hash: str) -> bool:
    """Return True iff `venv_dir` is a working venv built for `deps_hash`.

    Catches all the failure modes that have happened in practice:
      - bin/python missing (venv never built, or partial wipe)
      - pyvenv.cfg missing (venv structure incomplete — Python won't add
        the venv's site-packages to sys.path, so installed wheels are
        unreachable)
      - deps-hash marker missing or mismatched (deps tuple changed since
        the venv was built — needs reinstall)
      - `import mcp` fails under the venv's python (interpreter or
        site-packages broken, e.g. pyenv updated underneath us)

    `venv_dir` and `deps_hash` are arguments (not module globals) so this
    function is unit-testable with synthetic temp-dir fixtures.
    """
    venv_py = venv_dir / "bin" / "python3"
    if not venv_py.exists():
        return False
    if not (venv_dir / "pyvenv.cfg").exists():
        return False
    marker = venv_dir / _VENV_MARKER
    if not marker.exists():
        return False
    try:
        marker_value = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # Corrupt, root-owned, or non-UTF-8 marker → treat as invalid so
        # the bootstrap rebuilds instead of crashing with a traceback.
        return False
    if marker_value != deps_hash:
        return False
    try:
        subprocess.check_call(
            [str(venv_py), "-c", _venv_per_launch_probe()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_VENV_PER_LAUNCH_PROBE_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # Probe failed, timed out, or interpreter is unrunnable —
        # rebuild rather than try to limp along.
        return False
    return True


def _find_builder_python(venv_dir: Path) -> str:
    """Return a python3 executable suitable for building the venv at `venv_dir`.

    Prefer the interpreter that invoked us; fall back to common Homebrew /
    pyenv locations. Skips anything below `_VENV_MIN_PY`. `venv_dir` is
    used only for the error message — passed as a parameter so the
    function doesn't depend on a module-level `_VENV_DIR` snapshot.
    """
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
                timeout=10,  # probe is `sys.exit(0 if ver >= ...)` — <100ms in practice
            )
            return cand
        except (subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError):
            continue
    raise RuntimeError(
        f"No python3 >= {_VENV_MIN_PY[0]}.{_VENV_MIN_PY[1]} found to build "
        f"{venv_dir}; install one (e.g. via pyenv or `brew install python`) "
        f"and retry."
    )


def _looks_like_zh_venv(venv_dir: Path) -> bool:
    """Return True iff `venv_dir` is safe for `_build_venv` to rmtree.

    A user-controlled `ZH_MCP_VENV` (or a typo'd `XDG_DATA_HOME`) could
    point the venv path at any directory on disk — including `$HOME`, a
    project venv, or a regular file. Without this guard, a stale/wrong
    path plus a routine "venv looks invalid" verdict would silently
    `shutil.rmtree` arbitrary user data on the next launch.

    Safe to delete only when:
      - the path doesn't exist yet (nothing to remove)
      - the path is a directory AND empty (nothing to lose)
      - the path is a directory AND carries our `_VENV_MARKER`
        (a venv we built and certified) OR `_BUILD_SENTINEL` (a venv
        a prior `_build_venv` crashed in the middle of). The sentinel
        is what makes the bootstrap self-healing across crashes /
        Ctrl-C / SIGKILL mid-build: even with no marker, the sentinel
        certifies "we own this directory, safe to wipe and retry."
        `pyvenv.cfg` alone is NOT sufficient — the user may have
        pointed ZH_MCP_VENV at their own project's venv.

    Anything else (a regular file, an unreadable dir, a foreign venv,
    `~/Documents`) must be removed by the user explicitly.
    """
    if not venv_dir.exists():
        return True
    if not venv_dir.is_dir():
        return False  # regular file, FIFO, etc.
    try:
        is_empty = not any(venv_dir.iterdir())
    except OSError:
        return False  # unreadable, race-deleted, etc.
    if is_empty:
        return True
    return (
        (venv_dir / _VENV_MARKER).exists()
        or (venv_dir / _BUILD_SENTINEL).exists()
    )


def _ensure_safe_parent(venv_dir: Path, *, user_supplied: bool) -> None:
    """Validate the path's parent before any mkdir.

    Two modes, distinguished by the explicit `user_supplied` flag from
    `_default_venv_dir`:

    1. **Server-chosen default** (`user_supplied=False`): the path comes
       from `_default_venv_dir`'s XDG branch — `$XDG_DATA_HOME/zh/venv`,
       typically `~/.local/share/zh/venv`. `~/.local/share` is NOT
       guaranteed to exist on fresh macOS accounts, minimal Linux
       containers (python:slim, distroless, Alpine), DynamicUser=
       systemd sandboxes, or CI runners with bespoke $HOME. Auto-
       create the full ancestor tree; the path is server-controlled
       and not subject to typo risk.

    2. **User-supplied** (`user_supplied=True`): apply strict typo
       protection. Require the grandparent to pre-exist and only
       create ONE new directory level. A typo'd path like
       `ZH_MCP_VENV=~/something-typo/sub/venv` refuses rather than
       silently materializing a 500MB venv tree at an unexpected
       location.

    The flag is threaded as a parameter (not re-read from env at each
    call boundary) so the classification stays coupled to the path
    `_default_venv_dir` produced. Env mutation between calls can't
    defeat the typo guard.

    In both modes, a pre-existing regular file at the parent path
    raises a clear RuntimeError (avoiding the unhelpful
    `FileExistsError: [Errno 17]` traceback from `mkdir`).
    """
    parent = venv_dir.parent
    if parent.exists():
        # NOTE: `exists()` / `is_dir()` follow symlinks. A symlinked
        # parent (e.g. `~/.local/share` → a tmpfs-backed dir, or a
        # bespoke dotfile layout) is accepted as-is — we deliberately
        # do NOT reject symlinked parents, since that's a legitimate
        # and common setup. The downstream consequence is that if the
        # symlink's target vanishes between launches (tmpfs cleared on
        # reboot), the venv is simply rebuilt at the new target — no
        # data loss, just a one-time rebuild.
        if not parent.is_dir():
            raise RuntimeError(
                f"[zenhub-mcp] refusing to bootstrap into {venv_dir}: "
                f"parent {parent} exists but is not a directory. Remove "
                f"or rename the stray file, or set ZH_MCP_VENV / "
                f"XDG_DATA_HOME to point elsewhere."
            )
        return
    if not user_supplied:
        # Server-chosen XDG default. Auto-create the ancestor tree;
        # the path is server-controlled. Without this, a fresh user
        # account where `~/.local/share` doesn't exist yet would fail
        # to bootstrap — the most common first-launch failure on a
        # vanilla macOS install.
        parent.mkdir(parents=True, exist_ok=True)
        return
    grandparent = parent.parent
    if not grandparent.exists():
        raise RuntimeError(
            f"[zenhub-mcp] refusing to bootstrap into {venv_dir}: "
            f"ancestor {grandparent} does not exist. Auto-creating a "
            f"deep directory tree from a typo'd ZH_MCP_VENV would "
            f"silently pollute user space — create the parent "
            f"directory manually and retry."
        )
    # `exist_ok=True` because _ensure_safe_parent runs BEFORE the
    # bootstrap lock is acquired — two concurrent launches can both
    # pass the `parent.exists()` check above; the second one would
    # otherwise raise FileExistsError. The single-level-creation
    # safety property is enforced by the `grandparent.exists()` check
    # above, not by mkdir's mode.
    parent.mkdir(exist_ok=True)


def _safe_rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    """Robust rmtree that handles symlinks + a NARROW class of EACCES.

    Plain `shutil.rmtree` raises on symlinked dirs (`OSError: Cannot
    call rmtree on a symbolic link`) and on permission errors mid-walk.
    This wrapper:
      - unlinks symlinks instead of recursing (preserves the target)
      - on `PermissionError` during a walk, chmods BOTH the failing
        path AND its parent and retries
      - re-raises if a retry still fails, unless `ignore_errors=True`

    Recoverable cases:
      - file-unlink failure where the parent dir had write/execute
        bits dropped (chmod parent → retry unlink succeeds)
      - empty-dir rmdir failure where the dir itself had mode 000
        (chmod dir → retry rmdir succeeds)

    NOT recoverable here (despite the chmod retry firing):
      - `os.scandir` failure on a non-empty user-owned mode-000 dir.
        CPython's `shutil.rmtree` discards the retried iterator and
        treats the dir as empty, then `os.rmdir` raises `ENOTEMPTY`.
        Use `chmod -R u+rwx <path>` manually first.
      - any file/dir owned by another user (the chmod itself raises
        `PermissionError`).

    Cleanup-path callers (inside `_build_venv` except blocks) pass
    `ignore_errors=True` so a failed cleanup doesn't mask the
    original build failure.
    """
    try:
        if path.is_symlink():
            # Don't follow the symlink and rmtree the target! Just
            # remove the link itself.
            path.unlink()
            return

        # Bind the rmtree root in the closure so the chmod-parent
        # retry below can verify the parent it's about to chmod is
        # INSIDE the cleanup target. Round-3 #1: when shutil.rmtree
        # fails at the root itself (e.g. `os.scandir(venv_dir)` raises),
        # `os.path.dirname(p)` is `venv_dir.parent` — a directory
        # OUTSIDE our cleanup scope. Chmod-ing it silently downgrades
        # perms on a shared `~/.local/share/zh` or `/srv/shared/...`.
        # Never modify dirs outside the rmtree root.
        rmtree_root = path

        def _handle(func, p, exc):
            # `exc` is an exception instance (the unified form). Defensive
            # None guard: shutil can theoretically hand us something odd.
            if exc is not None and isinstance(exc, PermissionError):
                # Permission to unlink depends on the PARENT dir's
                # write+execute bits, not on the file's mode. Chmod the
                # parent ONLY if it sits at or inside the rmtree root —
                # never the root's own parent.
                p_parent = Path(p).parent
                if p_parent == rmtree_root or rmtree_root in p_parent.parents:
                    try:
                        os.chmod(str(p_parent), 0o700)
                    except OSError:
                        pass
                try:
                    os.chmod(p, 0o700)
                    func(p)
                    return
                except OSError:
                    pass
            raise exc

        # Python 3.12 deprecated `onerror=(func, path, exc_info_tuple)`
        # in favor of `onexc=(func, path, exc_instance)`, and emits a
        # DeprecationWarning to stderr on every call — which lands in
        # the MCP stdio transport's visible output. Dispatch by version
        # so 3.12+ uses onexc and 3.10/3.11 keep onerror. Both adapt to
        # the single `_handle(func, p, exc_instance)` shape.
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_handle)
        else:
            shutil.rmtree(
                path,
                onerror=lambda func, p, info: _handle(func, p, info[1]),
            )
    except OSError:
        if not ignore_errors:
            raise


def _build_venv(venv_dir: Path, deps_hash: str, *, user_supplied: bool) -> None:
    """Tear down (if present and safe) and rebuild the venv at `venv_dir`.

    Refuses to rebuild a directory that doesn't look like a venv (no
    `_VENV_MARKER`, no `_BUILD_SENTINEL`) to prevent destroying user
    data pointed at by a typo'd `ZH_MCP_VENV`. Writes a sentinel file
    at the start of the build so a crash mid-flight (Ctrl-C, SIGKILL,
    OOM, network failure, double-Ctrl-C-during-cleanup) leaves a
    self-recoverable state: `_looks_like_zh_venv` recognizes the
    sentinel and the next launch can safely rmtree + rebuild without
    manual cleanup. The marker is written only after the full
    post-build probe passes; sentinel is removed last.
    """
    if not _looks_like_zh_venv(venv_dir):
        raise RuntimeError(
            f"[zenhub-mcp] refusing to bootstrap into {venv_dir}: it "
            f"exists but does not look like a venv we built (no "
            f"{_VENV_MARKER} marker, no {_BUILD_SENTINEL} sentinel). "
            f"Check your ZH_MCP_VENV / XDG_DATA_HOME settings; remove "
            f"the directory manually if you really want it rebuilt."
        )
    builder = _find_builder_python(venv_dir)
    venv_py = venv_dir / "bin" / "python3"
    if venv_dir.exists():
        print(
            f"[zenhub-mcp] rebuilding {venv_dir} "
            f"(missing/broken or _VENV_DEPS changed)",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"[zenhub-mcp] bootstrapping {venv_dir} with {builder}",
            file=sys.stderr,
            flush=True,
        )
    _ensure_safe_parent(venv_dir, user_supplied=user_supplied)
    try:
        # rmtree lives INSIDE the try so a partial rmtree failure
        # (EACCES on a root-owned `__pycache__/*.pyc`, NFS stale
        # handle, antivirus quarantine race) hits the cleanup path
        # rather than escaping uncaught and bricking the next launch.
        if venv_dir.exists():
            _safe_rmtree(venv_dir)
        # Pre-create the venv dir and write the sentinel BEFORE any
        # subprocess. `python -m venv` preserves pre-existing files
        # in the target dir (it doesn't `--clear` by default), so the
        # sentinel survives the build. If anything between here and
        # the final marker write crashes — Ctrl-C, OOM, SIGKILL,
        # subprocess error — the sentinel stays on disk so the next
        # launch's _looks_like_zh_venv returns True and recovery is
        # automatic.
        venv_dir.mkdir()
        sentinel = venv_dir / _BUILD_SENTINEL
        # Sentinel CONTENT is irrelevant to correctness — only its
        # PRESENCE matters (it marks the dir as ours so a crashed build
        # is safely rebuildable). Deliberately no timestamp: a
        # machine-readable `started_at` would invite a future
        # "stale sentinel" age-check that breaks the simple
        # presence-means-ours invariant, and would record nonsense on
        # clock-skewed hosts. Human-readable note only.
        sentinel.write_text(
            "zenhub-cli MCP venv build in progress.\n"
            "Presence of this file marks the directory as ours, so a "
            "crashed/interrupted build can be safely wiped and rebuilt "
            "on the next launch. Safe to delete if no build is running.\n",
            encoding="utf-8",
        )
        subprocess.check_call(
            [builder, "-m", "venv", str(venv_dir)],
            timeout=_VENV_BUILD_TIMEOUT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install",
             "--quiet", "--no-cache-dir", "--upgrade", "pip"],
            timeout=_VENV_PIP_TIMEOUT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install",
             "--quiet", "--no-cache-dir", *_VENV_DEPS],
            timeout=_VENV_PIP_TIMEOUT,
        )
    except (KeyboardInterrupt, SystemExit):
        # First-time bootstrap takes minutes (torch is ~400MB). On
        # Ctrl-C / SIGTERM, attempt cleanup — but if that fails (e.g.
        # second Ctrl-C during rmtree), the sentinel on disk still
        # marks the dir as ours, so the next launch can recover
        # without manual intervention.
        _safe_rmtree(venv_dir, ignore_errors=True)
        raise
    except (subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError) as exc:
        # Build crashed mid-flight — clean up so the next launch's
        # _looks_like_zh_venv sees a missing path (safe to rebuild)
        # instead of needing the sentinel recovery path.
        _safe_rmtree(venv_dir, ignore_errors=True)
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = (f"timed out after {exc.timeout}s "
                      f"({exc.cmd[0] if exc.cmd else '?'} ...)")
        else:
            reason = f"build subprocess failed: {exc}"
        raise RuntimeError(
            f"[zenhub-mcp] venv build at {venv_dir} {reason}. Cleaned "
            f"up partial state; check your network (slow PyPI mirror?) "
            f"and Python installation, then retry."
        ) from exc
    # Post-build sanity check BEFORE writing the marker. Uses the FULL
    # probe (every declared dep, not just `mcp`) so a half-installed
    # venv where `sentence-transformers` failed mid-stream isn't
    # certified good. The per-launch probe stays light to avoid cold-
    # cache transformers-import rebuild loops.
    try:
        subprocess.check_call(
            [str(venv_py), "-c", _venv_full_probe()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_VENV_FULL_PROBE_TIMEOUT,
        )
    except (subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError) as exc:
        _safe_rmtree(venv_dir, ignore_errors=True)
        raise RuntimeError(
            f"[zenhub-mcp] venv at {venv_dir} was built but cannot "
            f"import all declared deps ({', '.join(_VENV_DEPS)}). "
            f"Cleaned up; retry the launch. Check your network and "
            f"Python installation."
        ) from exc
    # Atomic + durable marker write. os.replace makes the rename
    # atomic w.r.t. directory-entry visibility, but NOT durable: on
    # ext4 `data=ordered`, a power loss between the write and the
    # metadata commit can leave a zero-byte marker post-recovery —
    # which then fails the hash compare and triggers a rebuild loop
    # after every unclean shutdown. fsync the file contents before the
    # rename, and fsync the directory after, so the marker + its
    # directory entry are both on stable storage.
    marker = venv_dir / _VENV_MARKER
    tmp_marker = marker.with_suffix(".tmp")
    with open(tmp_marker, "w", encoding="utf-8") as fh:
        fh.write(deps_hash)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_marker, marker)
    try:
        dir_fd = os.open(str(venv_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Directory fsync is best-effort (not supported on every
        # filesystem). The file-level fsync + atomic rename already
        # give us the critical guarantee.
        pass
    # Sentinel last — the build is fully certified before we declare
    # it complete. A crash here leaves the marker (good) AND the
    # sentinel (also harmless — next launch will see the marker and
    # treat the venv as valid). Swallow non-FileNotFoundError too
    # (PermissionError, AV-scanner race) — the marker is the source
    # of truth at this point; the sentinel is cosmetic cleanup.
    try:
        sentinel.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def _venv_build_lock(venv_dir: Path, *, user_supplied: bool):
    """Serialize concurrent `_build_venv` invocations via fcntl.flock.

    Two MCP launches that fire within seconds (Claude Code session
    restart, supervisor reconnect, two terminal windows) used to race
    on rmtree + pip install with non-deterministic outcomes — one
    process could rmtree the other's in-flight site-packages, leaving
    a corrupt venv that the surviving process then certifies as
    valid. fcntl.flock on a parent-dir lockfile gives us serialization
    cheaply on POSIX. Callers should re-check `_venv_is_valid` AFTER
    acquiring the lock — another process may have completed the
    rebuild while we were waiting.

    Calls `_ensure_safe_parent` BEFORE creating the lock file so a
    typo'd `ZH_MCP_VENV` doesn't materialize an unintended ancestor
    tree just to host the lockfile. The lock file lands at
    `venv_dir.parent / _BOOTSTRAP_LOCK`.

    `user_supplied` is threaded from `_default_venv_dir` so the
    typo-protection in `_ensure_safe_parent` stays coupled to the
    path's origin.
    """
    _ensure_safe_parent(venv_dir, user_supplied=user_supplied)
    lock_path = venv_dir.parent / _BOOTSTRAP_LOCK
    # `O_NOFOLLOW` defeats the symlink-redirect attack vector that's
    # widest when ZH_MCP_VENV points at a shared-writable parent
    # (`/tmp`, `/srv/shared`, `/var/cache`). Without it, a local user
    # with write access to the parent could plant a symlink at
    # `_BOOTSTRAP_LOCK` → `~/.bashrc` (or any sensitive file).
    # Currently harmless because we only flock and never write, but
    # safe against future diagnostic-write additions.
    # `O_CLOEXEC` ensures the fd doesn't leak into the re-exec'd venv
    # python or any subprocess pip spawns.
    # Mode `0o600` keeps the lockfile single-user since it's per-process
    # coordination state, not data anyone else needs to read.
    fd = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _bootstrap_venv() -> None:
    # Compute paths at call time (not at module import) so a future
    # test that monkeypatches the env vars sees them — and so an
    # unreadable parent path doesn't crash module import for tests
    # that set ZH_MCP_SKIP_BOOTSTRAP=1.
    venv_dir, user_supplied = _default_venv_dir()
    venv_py = venv_dir / "bin" / "python3"
    deps_hash = _deps_hash()
    if not _venv_is_valid(venv_dir, deps_hash):
        # Serialize concurrent bootstraps. After acquiring the lock,
        # re-check validity — another process may have finished while
        # we were waiting, in which case skipping the rebuild saves
        # ~5 minutes of redundant torch download.
        with _venv_build_lock(venv_dir, user_supplied=user_supplied):
            if not _venv_is_valid(venv_dir, deps_hash):
                _build_venv(venv_dir, deps_hash, user_supplied=user_supplied)
    # Re-exec into the venv if we're not already running under it.
    # Compare sys.prefix (the canonical "which prefix am I running under")
    # to the venv dir, NOT realpath(sys.executable) vs realpath(venv_py)
    # — those resolve to the same target on Linux when the venv's bin/python
    # is a symlink to the same builder interpreter as the launcher, which
    # incorrectly skips the re-exec and leaves us running outside the venv.
    if Path(sys.prefix).resolve() != venv_dir.resolve():
        # Flush before execv: stderr is block-buffered under the MCP stdio
        # transport, so the "bootstrapping" / "rebuilding" messages would
        # otherwise be lost. `__file__` is resolved to an absolute path so
        # the child works even if a wrapper script changes cwd between
        # launch and bootstrap.
        sys.stderr.flush()
        sys.stdout.flush()
        argv = [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]]
        try:
            os.execv(str(venv_py), argv)
        except OSError:
            # TOCTOU: the venv was wiped between _venv_is_valid and
            # execv (concurrent rm, broken symlink, race-deleted bin/
            # by a stale clean-up script). Rebuild once and retry; if
            # the second exec also fails, let it propagate.
            print(
                f"[zenhub-mcp] {venv_py} missing at exec time; "
                f"rebuilding once and retrying...",
                file=sys.stderr,
                flush=True,
            )
            with _venv_build_lock(venv_dir, user_supplied=user_supplied):
                if not _venv_is_valid(venv_dir, deps_hash):
                    _build_venv(venv_dir, deps_hash, user_supplied=user_supplied)
            sys.stderr.flush()
            sys.stdout.flush()
            try:
                os.execv(str(venv_py), argv)
            except OSError as exc:
                # Second exec failed too — the venv was rebuilt but
                # still can't be exec'd (noexec mount, broken interpreter,
                # SELinux/AppArmor exec denial, or a second TOCTOU race).
                # Surface an actionable error instead of a bare OSError
                # traceback at MCP startup.
                raise RuntimeError(
                    f"[zenhub-mcp] failed to re-exec into {venv_py} even "
                    f"after rebuilding the venv: {exc}. The venv may be on "
                    f"a noexec mount or the interpreter is broken/blocked. "
                    f"Delete {venv_dir} and relaunch; if it persists, set "
                    f"ZH_MCP_VENV to a path on an exec-capable filesystem."
                ) from exc


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
# Matches `zh`'s per-issue header rows in mine / pipeline / etc. listings:
# `  #645 │ owner/repo │ ...` (2-space indent). Two tightening choices
# both implemented as `[ \t]` (horizontal whitespace only, never \n):
#   - Leading indent `^[ \t]{0,3}`: bounded to 0-3 so a 4-space title
#     line that legitimately starts with `#NNN │` (cross-reference
#     convention) isn't mistaken for the next header. Tab support is
#     defensive — `zh` currently emits spaces.
#   - Post-digit gap `\d+[ \t]*│`: previously `\s*` (which matches \n).
#     Pins the single-line-match contract — a header that wraps mid-
#     field (terminal resize, malformed unicode in a repo name) won't
#     span lines and trip the title-walker bail-out. The walker only
#     ever feeds one line at a time today; this is defense-in-depth.
_ISSUE_HEADER_RE = re.compile(r"^[ \t]{0,3}#\d+[ \t]*│")


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
        # Round-8 #3: align with timeout / success branches — `stderr`
        # and `stderr_plain` carry the same content, just with vs.
        # without ANSI escapes. This message is plain ASCII so the
        # two are identical, but callers comparing the fields (or
        # reading `stderr_plain` exclusively) must see the diagnostic.
        msg = f"zh binary not found at {ZH_BIN}"
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": msg,
            "stdout_plain": "",
            "stderr_plain": msg,
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
        # Round-6 #15: also strip ANSI from stderr_plain. MCP
        # callers reading stderr would otherwise see raw `\x1b[...m`
        # escape codes embedded in error messages. Same symmetric
        # fix applies to the success-return path below.
        #
        # Round-7 #5: align `stderr` and `stderr_plain` so they
        # describe the same subprocess state, just with vs without
        # ANSI escapes. Pre-fix `stderr` was the synthetic timeout
        # message only, while `stderr_plain` preferred the captured
        # `e.stderr` (when non-empty). The two diverged — callers
        # reading `stderr` lost any diagnostic the subprocess had
        # emitted before timing out. Now both fields contain the
        # captured diagnostic (when present) AND the synthetic
        # timeout suffix.
        captured_stderr = e.stderr or ""
        synthetic = (
            f"zh subprocess timed out after {timeout}s (args={args!r})"
        )
        combined_stderr = (
            f"{captured_stderr}\n{synthetic}"
            if captured_stderr else synthetic
        )
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": e.stdout or "",
            "stderr": combined_stderr,
            "stdout_plain": _ANSI_RE.sub("", e.stdout or ""),
            "stderr_plain": _ANSI_RE.sub("", combined_stderr),
        }
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_plain": _ANSI_RE.sub("", result.stdout),
        # Round-6 #15: symmetric ANSI strip on stderr — MCP callers
        # surfacing tool errors should see human-readable text, not
        # escape codes.
        "stderr_plain": _ANSI_RE.sub("", result.stderr),
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
            # Title is on the next indented non-empty line. Bail out
            # only when we see a real `#NNN │` header. Require the
            # candidate to actually start with whitespace so an
            # interstitial unindented banner (group header, gh warning,
            # progress note) can't be silently consumed as the title.
            title = ""
            j = i + 1
            while j < len(lines) and not _ISSUE_HEADER_RE.match(lines[j]):
                line = lines[j]
                if line.startswith((" ", "\t")):
                    stripped = line.strip()
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


def _parse_mine_listing(plain: str) -> list[dict]:
    """Parse `zh mine` output into list of {number, repo, pipeline, title}.

    `zh mine` uses a different per-issue shape than `zh pipeline`: three
    fields (`#N │ owner/repo │ pipeline`) instead of four (`#N │ repo │
    N pts │ assignee`), because the assignee is implied by the command
    and the pipeline is the differentiating field. Separate parser keeps
    each regex specific rather than gluing both shapes into one
    branchier pattern.
    """
    issues: list[dict] = []
    lines = plain.splitlines()
    i = 0
    while i < len(lines):
        # Pipeline capture group is `[^│]+?` (not `.+?`) so it can't
        # cross another `│` separator. If `zh mine` ever grows a 4th
        # column the line stops matching here — better than the lazy
        # `.+?` silently swallowing `"<pipeline> │ <title>"` into
        # `pipeline` and leaving `title=""` (silent data corruption).
        m = re.match(
            r"^\s*#(\d+)\s*│\s*(\S+/\S+)\s*│\s*([^│]+?)\s*$",
            lines[i],
        )
        if m:
            number = int(m.group(1))
            repo = m.group(2)
            pipeline = m.group(3).strip()
            # Title is the next indented non-empty non-arrow line. Bail
            # only on a real `#NNN │ ...` header (titles can start with
            # `#`), and require the candidate to start with whitespace
            # so an interstitial unindented banner can't be silently
            # swallowed as the title.
            title = ""
            j = i + 1
            while j < len(lines) and not _ISSUE_HEADER_RE.match(lines[j]):
                line = lines[j]
                if line.startswith((" ", "\t")):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("→"):
                        title = stripped
                        break
                j += 1
            issues.append({
                "number": number,
                "repo": repo,
                "pipeline": pipeline,
                "title": title,
            })
        i += 1
    return issues


# Sub-issue helpers moved to zh_graphql_ops.py — the MCP server now talks
# directly to ZenHub's GraphQL API for the sub-issue family of tools
# (list / add / remove / reorder). The bash text contract used in v1.5.0
# was a recurring source of drift; v1.6.0 retires it entirely.


# v1.9.0 retired `_parse_new_issue_number` / `_parse_new_epic_number` and
# their `_SUCCESS_*_RE` anchors. Every create path (issue + planning nouns)
# now invokes `zh ... create --json`, which writes a clean JSON object to
# stdout (human chatter goes to stderr); `_parse_create_json` below parses
# that JSON. The colorized success-line scrape is the failure mode (G2)
# this migration was built to eliminate.


def _parse_create_json(plain: str) -> dict | None:
    """Parse the JSON object emitted by `zh create --json` / `zh <noun>
    create --json`.

    With --json, `zh` sends all human chatter to stderr and writes a
    single JSON object to stdout (number, url, title, type, pipeline,
    estimate, parent). `_run_zh` only captures stdout in `stdout_plain`,
    so this should be pure JSON; we still scan for the first balanced
    object defensively in case a wrapper prepends anything.

    Returns the parsed dict, or None if no JSON object is found.
    """
    if not plain:
        return None
    text = plain.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    # Defensive: find the first {...} block and try again.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


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
        dict with:
            ok: bool
            issues: list of {number, repo, pipeline, title}
            raw: full stdout for display
            stderr: any error output
    """
    args = ["mine"]
    if user:
        args.append(user)
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "issues": _parse_mine_listing(r["stdout_plain"]) if r["ok"] else [],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def epic_list(repo_path: str = "") -> dict:
    """List issues of type Epic in the workspace (v1.9.0 model).

    An epic is a normal issue whose ZenHub issue-type is Epic; this lists
    every such issue. The `number` is an ordinary GitHub issue number with
    a normal issue URL (the old ZenhubEpic id concept is gone).

    v1.9.0 delegates to the generic `_planning_list` so initiative_list /
    project_list / subtask_list all behave identically. The response
    exposes BOTH the new noun-neutral `items` key AND a back-compat
    `epics` alias (deprecated) for callers pinned to the v1.8.x shape.

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, items (list of {number, state, title}),
        epics (back-compat alias for items, deprecated), raw, stderr.
    """
    result = _planning_list("epic", repo_path)
    # Back-compat alias: every other planning noun returns `items`; v1.8.x
    # callers of epic_list read `epics`. Carry both.
    result["epics"] = result.get("items", [])
    return result


@mcp.tool()
def epic_show(epic_number: int, repo_path: str = "") -> dict:
    """Show full detail for an epic issue (metadata + child issues).

    v1.9.0: `epic_number` is an ordinary GitHub issue number (the issue
    whose type is Epic). Children are the issue's sub-issues.

    Args:
        epic_number: Issue number of the Epic-typed issue.
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, epic_number, raw (the full formatted output), stderr.
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
    """List the workspace's assignable issue types with level + disposition.

    v1.9.0: backed by assignableIssueTypes, so this shows the full 5-level
    hierarchy (Initiative / Project / Epic at PLANNING_PANEL, plus Bug /
    Feature / Task / Sub-task at BOARD), each with its level (1-5),
    disposition, and source (ZenhubIssueType vs GithubIssueType). The old
    listing only saw the board-level GitHub types.

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
        return None, _similarity_exc_to_stderr(e)


def _similarity_exc_to_stderr(exc: Exception) -> str:
    """Turn a similarity-path exception into an actionable message.

    A `ModuleNotFoundError` / `ImportError` here means the MCP venv's
    embedding dependencies (sentence-transformers / torch / numpy) are
    missing or corrupted. The per-launch bootstrap probe only checks
    the primary dep (`mcp`), so a partial-dep corruption between
    launches isn't caught until the first similarity call. Rather than
    surface a bare `No module named 'sentence_transformers'`, point the
    caller at the one-line fix (delete the venv → next launch rebuilds).
    """
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        try:
            venv_hint = str(_default_venv_dir()[0])
        except Exception:
            venv_hint = "the MCP venv ($ZH_MCP_VENV / $XDG_DATA_HOME/zh/venv)"
        return (
            f"similarity dependencies unavailable in the MCP venv "
            f"({exc}). The venv appears to have missing or corrupted "
            f"embedding deps. Delete it and relaunch to trigger a clean "
            f"rebuild: rm -rf {venv_hint}"
        )
    return str(exc)


@mcp.tool()
def zh_similar(query: str, top_k: int = 5, threshold: float = 0.35,
               repo_path: str = "") -> dict:
    """Find issues semantically similar to a query string.

    Uses sentence-transformer embeddings (all-MiniLM-L6-v2) over the
    titles + body previews of every open issue in the repo. Catches
    paraphrased duplicates that keyword search misses.

    The cache auto-refreshes on a 5-minute TTL via GitHub's
    `?since=<ISO8601>` filter — only changed issues get re-embedded.
    First call after a wipe (or first call ever) triggers a full pull
    and may take 30-60s depending on backlog size. No manual reindex
    is needed; the sync is transparent on every call.

    ALWAYS returns the top-K closest issues (never a bare empty list
    when the repo has any open issues). Each match carries
    `meets_threshold`: True when its score cleared `threshold`, False
    when it's surfaced only as a closest-candidate. Use the top-level
    `any_above_threshold` for a quick "was there a strong match?" read.

    Tip: natural-language queries ("admin wizard dark-mode contrast
    bug") embed more tightly than keyword salads ("contrast dark admin")
    and score higher. The default threshold (0.35) is tuned for short
    ad-hoc lookups.

    Args:
        query: text to compare against existing issues. Free-form;
            full sentences work better than disconnected keywords.
        top_k: max results to return (default 5).
        threshold: cosine similarity (0.0-1.0) at/above which a match is
            flagged `meets_threshold=True` (default 0.35).
        repo_path: Optional absolute path of a git checkout. Used to
            derive `owner/repo` for the search.

    Returns:
        dict with: ok, repo, threshold, any_above_threshold, matches
        (list of {number, repo, title, body_preview, state, similarity,
        meets_threshold}), stderr.
    """
    repo, err = _similarity_repo(repo_path)
    if err:
        return {"ok": False, "matches": [], "stderr": err}
    try:
        from similarity import find_similar

        # min_results=top_k → always backfill to the closest top_k so the
        # caller sees the nearest candidates (annotated) instead of [].
        results = find_similar(
            query, repo, top_k=top_k, threshold=threshold,
            min_results=top_k, auto_sync=True,
        )
        return {
            "ok": True,
            "repo": repo,
            "threshold": threshold,
            "any_above_threshold": any(m.meets_threshold for m in results),
            "matches": [m.to_dict() for m in results],
            "stderr": "",
        }
    except Exception as e:
        return {"ok": False, "repo": repo, "matches": [],
                "stderr": _similarity_exc_to_stderr(e)}


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
        return {"ok": False, "repo": repo,
                "stderr": _similarity_exc_to_stderr(e)}


# -----------------------------------------------------------------------------
# WRITE TOOLS — ISSUE LIFECYCLE
# -----------------------------------------------------------------------------

@mcp.tool()
def create_issue(title: str, body: str, type: str = "Task",
                 pipeline: str = "Product Backlog",
                 labels: str = "", parent: int = 0,
                 priority: str = "", repo_path: str = "",
                 confirm_create: bool = False,
                 skip_duplicate_check: bool = False) -> dict:
    """Create a new ZenHub issue.

    Runs a pre-flight similarity check against existing open issues
    (via the sentence-embedding index, see `zh_similar`). When a
    near-duplicate is detected (cosine similarity above the hard
    threshold), the create is BLOCKED and the candidate matches are
    returned. Pass `confirm_create=True` to override and create
    anyway. Soft matches are surfaced as a warning but don't block.

    v1.9.0: `type` is resolved via assignableIssueTypes, so any
    configured type works (Bug / Feature / Task at board level, plus
    the planning-panel types Initiative / Project / Epic and Sub-task).
    A type name that is not assignable in the workspace is now a hard
    error (the underlying `zh create` lists the available types) rather
    than a silently-typeless create. Use list_types to discover them.

    Args:
        title: Issue title (required, non-empty).
        body: Issue body / description in Markdown (required, non-empty).
        type: Issue type. Defaults to Task. Discover with list_types.
        pipeline: Target pipeline. Defaults to "Product Backlog".
        labels: Comma-separated label names (optional).
        parent: Optional parent issue number. When > 0 the new issue is
            wired as a sub-issue of that parent (ZenHub-native
            addSubIssues, so it shows up under epic_show / subissue
            reads).
        priority: Optional priority name (resolved case-insensitively
            against the workspace's configured priorities; discover
            names with list_priorities). Round-6 finding #6: same
            surface the bash `--priority` flag exposes. When set, the
            response carries `priority_requested` mirroring the input
            and `priority` reflecting the post-create mutation
            confirmation. Compare the two to detect partial apply.
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
            type, pipeline, parent, estimate, estimate_requested,
            priority, priority_requested, raw, stderr, duplicate_check
            (informational; may include soft matches). v1.9.2 round-7
            finding #3: `estimate_requested` mirrors the priority
            request/applied split. Compare it against `estimate`:
            null/null = not requested, N/N = applied, N/null =
            requested but the setEstimate mutation did not confirm
            (retry, do NOT assume it landed).
    """
    # v1.9.2 round-2 (PR #27) finding #2: validation early returns
    # must match the full documented key set so clients reading
    # out["number"] / out["raw"] / out["estimate_requested"] / etc.
    # per the docstring contract do not KeyError on a bad-input call.
    # Same shape-drift family as round-7 #8 / #11 (which fixed
    # set_issue_type and _planning_update); the create_issue sibling
    # validation paths were left at the old 2-key shape.
    _empty_create_shape = {
        "ok": False,
        "number": None,
        "url": None,
        "type": None,
        "pipeline": None,
        "parent": None,
        "estimate": None,
        "estimate_requested": None,
        "priority": None,
        "priority_requested": None,
        "raw": "",
    }
    if not title.strip():
        return {**_empty_create_shape, "stderr": "title must be non-empty"}
    if not body.strip():
        return {**_empty_create_shape, "stderr": "body must be non-empty"}

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
                # Embedding failure shouldn't block create — log only,
                # with an actionable hint if the venv deps are corrupt.
                dup_info = {
                    "ok": False,
                    "stderr": (
                        "duplicate check failed: "
                        + _similarity_exc_to_stderr(e)
                    ),
                    "matches": [],
                }

        if (dup_info and dup_info.get("recommendation") == "block"
                and not confirm_create):
            # v1.9.2 round-7 finding #7: full key-set on the blocked
            # path so create_issue and the planning-noun creates
            # behave the same way for clients reading documented keys
            # like out["number"], out["estimate_requested"], etc.
            return {
                "ok": False,
                "blocked": True,
                "number": None,
                "url": None,
                "type": None,
                "pipeline": None,
                "parent": None,
                "estimate": None,
                "estimate_requested": None,
                "priority": None,
                "priority_requested": None,
                "raw": "",
                "stderr": (
                    "Refused: a similar open issue already exists "
                    "(cosine similarity >= "
                    f"{dup_info.get('hard_threshold')}). "
                    "Review duplicate_check.matches; if the new ticket is "
                    "genuinely distinct, retry with confirm_create=True."
                ),
                "duplicate_check": dup_info,
            }

    # v1.9.0: use --json so the new number is parsed from a clean JSON
    # object on stdout rather than scraped from a colorized success line
    # (the parse-miss that motivated G2).
    args = ["create", title, "-t", type, "-p", pipeline, "-b", body, "--json"]
    if labels:
        args.extend(["-l", labels])
    if parent and parent > 0:
        args.extend(["--parent", str(parent)])
    if priority:
        args.extend(["--priority", priority])
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))

    # Round-6 finding #6: shape parity with _planning_create. The bash
    # --json emit carries priority / priority_requested / estimate;
    # propagate them all so MCP clients reading either create surface
    # see the same key set.
    created = _parse_create_json(r["stdout_plain"]) if r["ok"] else None
    out = {
        "ok": r["ok"] and created is not None,
        "number": created.get("number") if created else None,
        "url": created.get("url") if created else None,
        "type": created.get("type") if created else None,
        "pipeline": created.get("pipeline") if created else None,
        "parent": created.get("parent") if created else None,
        "estimate": created.get("estimate") if created else None,
        # v1.9.2 round-7 finding #3: bash --json emits the three-state
        # estimate split (estimate / estimate_requested). Propagate the
        # `_requested` half so MCP callers can detect a setEstimate
        # mutation that lost the value (estimate=null, requested=N).
        "estimate_requested": (
            created.get("estimate_requested") if created else None
        ),
        "priority": created.get("priority") if created else None,
        "priority_requested": (
            created.get("priority_requested") if created else None
        ),
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
        # v1.9.2 round-3 (PR #27) finding #1: validation early-return
        # must match the success-path key set (number, raw, stderr)
        # so clients reading out["number"] / out["raw"] per the
        # docstring do not KeyError on a bad-input call. Same drift
        # family the PR closed for create_issue (round-2 #2),
        # _planning_create (round-2 #3), _planning_update (round-7
        # #11), and set_issue_type (round-7 #8). `comment()` was the
        # surviving sibling.
        return {"ok": False, "number": number, "raw": "",
                "stderr": "message must be non-empty"}
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
    """Set or clear an issue's priority by name.

    v1.9.0 (G1): priorities are workspace-defined, not a fixed
    high/medium/low set. `level` is matched case-insensitively against
    the workspace's configured priority names; pass "clear" to remove.
    Discover the configured names with list_priorities. If no priority
    matches, this fails with the available names listed in stderr (the
    underlying `zh priority` no longer silently sets the first priority).

    Args:
        number: Issue number.
        level: A configured priority name (e.g. "High priority") or
            "clear" to remove the priority.
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


@mcp.tool()
def list_priorities(repo_path: str = "") -> dict:
    """List the workspace's configured priorities (G1 companion).

    Priorities are workspace-defined. Use this to discover the names that
    set_priority accepts.

    Args:
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, raw (formatted priority listing), stderr.
    """
    r = _run_zh(["priorities"], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


@mcp.tool()
def set_issue_type(number: int, issue_type: str, repo_path: str = "") -> dict:
    """Change an existing issue's type (G8).

    v1.9.0: wraps changeIssueTypeOfIssues, which accepts the unified type
    id for both GithubIssueType and ZenhubIssueType, so this can promote a
    board issue to a planning-panel Epic (or any configured type) and back.
    The type name is resolved via assignableIssueTypes (discover with
    list_types); an unknown type fails with the available names in stderr.

    Args:
        number: Issue number to retype.
        issue_type: Target type name (e.g. "Epic", "Feature").
        repo_path: Optional absolute path of a git checkout to run zh from.

    Returns:
        dict with: ok, partial_applied, number, issue_type, raw, stderr.

        v1.9.2 round-2 #6 (compat note): `ok` is True when EITHER the
        underlying call succeeded cleanly OR the partial-applied path
        fired. This is a semantic flip from v1.9.1 (which surfaced
        the partial as `ok=False`). Callers that branch ONLY on `ok`
        miss the partial signal and skip re-verification of an
        operation whose follow-on errors should be inspected before
        the next mutation. Correct idiom from v1.9.2 on:
            if r["partial_applied"]: re-verify with zh issue <number>
            elif r["ok"]: trust the type change
            else: hard failure, safe to retry
        Same shape as the planning-children wrappers (round-7 #10).

        v1.9.2 round-7 finding #9: `partial_applied` is True when the
        underlying `zh type` exited 2 (round-6 #4 convention: ZenHub
        side accepted the change but the mutation reported follow-on
        errors). Agents that retry on ok=False MUST branch on
        partial_applied — a retry of a partial-applied change runs a
        second mutation against an issue whose type already changed,
        which can no-op or fail in confusing ways. Re-verify the
        type with `zh issue N` before deciding.
    """
    if not issue_type.strip():
        # v1.9.2 round-7 finding #8: validation early-return must
        # include partial_applied so clients that uniformly key-check
        # `out["partial_applied"]` do not KeyError.
        return {"ok": False, "partial_applied": False,
                "number": number, "issue_type": issue_type,
                "raw": "", "stderr": "issue_type must be non-empty"}
    r = _run_zh(["type", str(number), issue_type],
                cwd=_resolve_cwd(repo_path))
    # Round-6 finding #4: surface the exit-code convention. The bash
    # cmd_set_type uses exit 2 for the divergence-only partial case
    # (ZenHub side accepted the type change but the mutation also
    # reported a GitHub-side error or a populated failedIssues). For
    # MCP callers the distinction matters: a true failure (exit 1) is
    # safe to retry, but a partial apply (exit 2) means the change
    # already landed and a retry would be wasted (or hit a no-op
    # error). Expose `partial_applied: True` so an agent can branch.
    partial_applied = int(r.get("exit_code") or 0) == 2
    return {
        "ok": r["ok"] or partial_applied,
        "partial_applied": partial_applied,
        "number": number,
        "issue_type": issue_type,
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
# WRITE TOOLS: PLANNING HIERARCHY (issue-type + sub-issue model, v1.9.0)
#
# Each planning level (initiative / project / epic / subtask) is the SAME
# machinery parameterised by a ZenHub issue-type name. The private _planning_*
# helpers shell to the matching `zh <noun>` subcommand; the public @mcp.tool()
# functions are thin per-noun wrappers, mirroring the data-driven bash design.
# An "epic" is a normal issue whose issue-type is Epic; children are wired with
# Sub-Issues, so create/add/show/etc. all act on real issue numbers and normal
# issue URLs. The dead ZenhubEpic API the old epic_* tools targeted is gone.
# -----------------------------------------------------------------------------


def _planning_create(noun: str, title: str, description: str, labels: str,
                     pipeline: str, assignee: str, estimate: str,
                     parent: int, repo_path: str,
                     confirm_create: bool = False,
                     skip_duplicate_check: bool = False) -> dict:
    """Shared `zh <noun> create --json` wrapper for the planning nouns.

    Forwards every meaningful create-time flag the bash side exposes:
    description, labels, pipeline, assignee, estimate, parent. The
    duplicate-check pre-flight mirrors create_issue (v1.9.1 item #5):
    every planning-noun create now runs the same similarity guard, so
    an agent calling `epic_create("Auth redesign")` gets blocked on a
    near-duplicate the same way `create_issue(..., type="Epic")` would.

    Args:
        noun: planning noun (initiative / project / epic / subtask).
        title: issue title.
        description: optional body / description.
        labels, pipeline, assignee, estimate: optional create-time flags.
        parent: optional parent issue number (0 = none).
        repo_path: optional absolute path of a git checkout.
        confirm_create: pass True to bypass a duplicate-check block.
        skip_duplicate_check: pass True to skip the pre-flight entirely.
    """
    if not title.strip():
        # v1.9.2 round-2 (PR #27) finding #3: full key-set so the
        # validation path matches the success / blocked-path shape.
        # Missing keys (estimate_requested, priority,
        # priority_requested, raw) were the same drift class as
        # the round-2 #2 fix for create_issue.
        return {
            "ok": False,
            "number": None,
            "url": None,
            "type": None,
            "pipeline": None,
            "parent": None,
            "estimate": None,
            "estimate_requested": None,
            "priority": None,
            "priority_requested": None,
            "raw": "",
            "stderr": "title must be non-empty",
        }

    # v1.9.1 item #5: pre-flight similarity check, identical machinery
    # to create_issue. Same shape: a "block" recommendation
    # short-circuits the create with the candidate matches; "warn" only
    # annotates the response. Embedding failures or missing index fall
    # through to create rather than blocking, so a transient infra
    # problem cannot become a planning-noun outage.
    #
    # Round-3 finding #8: the two entry points differ on input
    # validation. create_issue requires a non-empty body, so its
    # embedding always sees both title and body. _planning_create
    # accepts an empty description (planning items are often title-
    # only). At the SOFT/HARD threshold boundary the same title can
    # therefore land on different sides of the gate from the two
    # surfaces. Equalizing this would mean either tightening
    # _planning_create (hurts UX for legitimate title-only initiatives
    # / epics) or relaxing create_issue (lowers a useful hint). The
    # tradeoff is documented here so a future maintainer sees the
    # asymmetry rather than treating it as a bug; agents that need
    # identical scoring across surfaces should always pass description.
    dup_info = None
    if not skip_duplicate_check:
        repo, err = _similarity_repo(repo_path)
        if err:
            dup_info = {"ok": False, "stderr": err, "matches": []}
        else:
            try:
                from similarity import check_duplicate

                dup_info = check_duplicate(title, description, repo)
            except Exception as e:
                dup_info = {
                    "ok": False,
                    "stderr": (
                        "duplicate check failed: "
                        + _similarity_exc_to_stderr(e)
                    ),
                    "matches": [],
                }

        if (dup_info and dup_info.get("recommendation") == "block"
                and not confirm_create):
            # v1.9.2 round-7 finding #7: the round-2 #6 fix dropped
            # this branch to a 4-key shape (ok / blocked / stderr /
            # duplicate_check). That worked for clients that only
            # read create_issue's blocked path (whose docstring is
            # explicit about the truncation), but the planning-noun
            # docstrings list the full 10-key contract. Agents
            # reading `out["number"]` per the documented shape would
            # KeyError on a blocked initiative_create / project_create
            # / subtask_create. epic_create was partly insulated by
            # _with_epic_number_alias setting `epic_number=None`, but
            # only that one key was patched; the others stayed
            # missing. Restore the full shape with None placeholders
            # so every documented key is present.
            return {
                "ok": False,
                "blocked": True,
                "number": None,
                "url": None,
                "type": None,
                "pipeline": None,
                "parent": None,
                "estimate": None,
                "estimate_requested": None,
                "priority": None,
                "priority_requested": None,
                "raw": "",
                "stderr": (
                    "Refused: a similar open issue already exists "
                    "(cosine similarity >= "
                    f"{dup_info.get('hard_threshold')}). "
                    "Review duplicate_check.matches; if the new ticket is "
                    "genuinely distinct, retry with confirm_create=True."
                ),
                "duplicate_check": dup_info,
            }

    args = [noun, "create", title, "--json"]
    if description:
        args.extend(["-d", description])
    if labels:
        args.extend(["-l", labels])
    if pipeline:
        args.extend(["-p", pipeline])
    if assignee:
        args.extend(["-a", assignee])
    if estimate:
        args.extend(["-e", estimate])
    if parent and parent > 0:
        args.extend(["--parent", str(parent)])
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    created = _parse_create_json(r["stdout_plain"]) if r["ok"] else None
    out = {
        "ok": r["ok"] and created is not None,
        "number": created.get("number") if created else None,
        "url": created.get("url") if created else None,
        "type": created.get("type") if created else None,
        "pipeline": created.get("pipeline") if created else None,
        "parent": created.get("parent") if created else None,
        "estimate": created.get("estimate") if created else None,
        # v1.9.2 round-7 finding #4: mirror create_issue's
        # estimate_requested propagation so epic_create /
        # initiative_create / project_create / subtask_create all
        # expose the three-state estimate signal. Without this an
        # agent calling `epic_create(estimate="5")` against a
        # transient setEstimate failure sees estimate=null with no
        # way to tell "didn't ask" from "asked but lost".
        "estimate_requested": (
            created.get("estimate_requested") if created else None
        ),
        # Round-4 finding #7: propagate the priority fields so the
        # planning-noun create returns the same key set create_issue
        # does. Planning creates do not accept --priority today, so
        # these will normally be null; including them keeps shape parity
        # against the future case where they DO get a priority flag,
        # and lets MCP clients read both entry points uniformly.
        "priority": created.get("priority") if created else None,
        "priority_requested": (
            created.get("priority_requested") if created else None
        ),
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }
    if dup_info is not None:
        out["duplicate_check"] = dup_info
    return out


def _planning_list(noun: str, repo_path: str) -> dict:
    """Shared `zh <noun> list` wrapper. Parses the `#NN STATE title` rows."""
    r = _run_zh([noun, "list"], cwd=_resolve_cwd(repo_path))
    items = []
    if r["ok"]:
        for line in r["stdout_plain"].splitlines():
            m = re.match(r"^\s*#(\d+)\s+(OPEN|CLOSED)\s+(.+?)\s*$", line)
            if m:
                items.append({
                    "number": int(m.group(1)),
                    "state": m.group(2),
                    "title": m.group(3).strip(),
                })
    return {
        "ok": r["ok"],
        "items": items,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _planning_show(noun: str, number: int, repo_path: str) -> dict:
    r = _run_zh([noun, "show", str(number)], cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _planning_update(noun: str, number: int, title: str, description: str,
                     repo_path: str) -> dict:
    if not title and not description:
        # v1.9.2 round-7 finding #11: validation early-return must
        # match the success-path shape so clients reading out["raw"]
        # per the docstring (epic_update / initiative_update / ...)
        # do not KeyError. Same shape-drift family as F8/F9.
        return {"ok": False,
                "stderr": "Must provide title and/or description",
                "number": number,
                "raw": ""}
    args = [noun, "update", str(number)]
    if title:
        args.extend(["-t", title])
    if description:
        args.extend(["-d", description])
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    return {
        "ok": r["ok"],
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _planning_add_children(noun: str, parent: int, children: list[int],
                           repo_path: str) -> dict:
    if not children:
        # v1.9.2 round-1 (PR #27) finding #9: include `raw` for shape
        # parity with the success / partial paths.
        return {"ok": False, "stderr": "issue_numbers must be non-empty",
                "parent": parent, "added": [], "added_requested": [],
                "partial_applied": False, "raw": ""}
    args = [noun, "add", str(parent)] + [str(n) for n in children]
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    # v1.9.2 round-7 finding #10: cmd_subissue_add exits 2 on a
    # divergence-only partial (some children attached, others didn't).
    # The pre-fix code collapsed both non-zero codes to ok=False,
    # added=[], which an agent reads as total failure. Retrying a
    # total-failure of a partial-success produces double-adds for the
    # children that DID land. Surface partial_applied analogous to
    # set_issue_type so the agent can branch.
    #
    # v1.9.2 round-1 (PR #27) finding #3 + round-2 finding #5: the
    # contract is now:
    #   - `added`: confirmed-landed list. On full success: == input.
    #     On partial: empty (the bash wrapper can't enumerate
    #     per-issue; the agent must consult subissue_list).
    #     On hard failure: empty.
    #   - `added_requested`: the input list, ALWAYS. Mirrors the
    #     estimate_requested / priority_requested pattern from
    #     create_issue: agents that want to know what was attempted
    #     read this field; agents that want what's confirmed read
    #     `added`. Past-tense `added` no longer overstates on partial.
    # On partial, `ok=True or partial_applied=True` (mirrors
    # set_issue_type). An agent's correct idiom is:
    #     if r["partial_applied"]: re-verify via subissue_list
    #     elif r["ok"]: log(f"Added {len(r['added'])} children")
    #     else: log(f"Failed (requested: {r['added_requested']})")
    partial_applied = int(r.get("exit_code") or 0) == 2
    return {
        "ok": r["ok"] or partial_applied,
        "partial_applied": partial_applied,
        "parent": parent,
        "added": children if r["ok"] else [],
        "added_requested": children,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _planning_remove_children(noun: str, parent: int, children: list[int],
                              repo_path: str) -> dict:
    if not children:
        return {"ok": False, "stderr": "issue_numbers must be non-empty",
                "parent": parent, "removed": [], "removed_requested": [],
                "partial_applied": False, "raw": ""}
    args = [noun, "remove", str(parent)] + [str(n) for n in children]
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    # v1.9.2 round-7 finding #10: same partial signal as the add path.
    # v1.9.2 round-2 (PR #27) finding #5: same removed/removed_requested
    # split — on partial, `removed` is empty (verify via
    # subissue_list), `removed_requested` is the input list always.
    partial_applied = int(r.get("exit_code") or 0) == 2
    return {
        "ok": r["ok"] or partial_applied,
        "partial_applied": partial_applied,
        "parent": parent,
        "removed": children if r["ok"] else [],
        "removed_requested": children,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _planning_close(noun: str, number: int, comment: str,
                    repo_path: str) -> dict:
    args = [noun, "close", str(number)]
    if comment:
        args.append(comment)
    r = _run_zh(args, cwd=_resolve_cwd(repo_path))
    # v1.9.2 round-3 (PR #27) finding #8: include `partial_applied`
    # for shape parity with set_issue_type and the planning-children
    # wrappers. cmd_close uses `gh` and has no exit-2 partial today,
    # so the field is always False here — but agents that uniformly
    # read out["partial_applied"] on every write tool should not
    # KeyError on a close.
    return {
        "ok": r["ok"],
        "partial_applied": False,
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _planning_reopen(noun: str, number: int, repo_path: str) -> dict:
    r = _run_zh([noun, "reopen", str(number)], cwd=_resolve_cwd(repo_path))
    # v1.9.2 round-3 (PR #27) finding #8: same `partial_applied`
    # shape-parity addition. Always False; reopen has no partial path.
    return {
        "ok": r["ok"],
        "partial_applied": False,
        "number": number,
        "raw": r["stdout_plain"],
        "stderr": r["stderr"],
    }


def _with_epic_number_alias(d: dict) -> dict:
    """Add an `epic_number` alias to an epic_* tool response (review #1).

    Pre-v1.9.0 the `epic_*` MCP tools returned `epic_number` in their dict;
    the rewrite to the generic `_planning_*` helpers uses `number` / `parent`
    instead. To avoid silently breaking agents pinned to the v1.8.x contract,
    every epic_* response carries an `epic_number` alias mirroring whichever
    of `number` or `parent` is the epic's identifier in that shape. Tool
    docstrings note the alias is for back-compat and may be removed in a
    future major release.

    v1.9.1 round-3 finding #3: the round-2 #6 fix minimized the
    `_planning_create` blocked-response to 4 keys (ok / blocked / stderr
    / duplicate_check), dropping the `number` and `parent` placeholders
    the alias used to set. Without a fallback, a v1.8.x agent reading
    `out["epic_number"]` on a blocked create gets `KeyError`. Always
    ensure the alias key is present (None when no identifier is
    available, e.g. on blocked / error paths).
    """
    if "number" in d and "epic_number" not in d:
        d["epic_number"] = d.get("number")
    elif "parent" in d and "epic_number" not in d:
        d["epic_number"] = d.get("parent")
    # Fallback for paths that carry neither (blocked / pre-flight
    # errors). v1.8.x clients use `out["epic_number"]` directly; this
    # guarantees the key exists.
    d.setdefault("epic_number", None)
    return d


# ---- Epic (the headline noun; backward-compatible tool names) ---------------
#
# Every epic_* tool returns the new generic shape plus an `epic_number` alias
# for back-compat with v1.8.x callers (see _with_epic_number_alias).

@mcp.tool()
def epic_create(title: str, description: str = "", labels: str = "",
                pipeline: str = "", assignee: str = "", estimate: str = "",
                parent: int = 0, repo_path: str = "",
                confirm_create: bool = False,
                skip_duplicate_check: bool = False) -> dict:
    """Create an Epic (an issue with issue-type Epic).

    v1.9.0: an epic is a normal issue typed Epic, with a normal issue
    number and URL (the ZenhubEpic id concept is gone). Every `zh create`
    flag is forwarded.

    v1.9.1 item #5: the duplicate-check pre-flight that create_issue runs
    now applies here too. A blocked match short-circuits the create with
    the candidate matches; pass `confirm_create=True` to override after
    reviewing, or `skip_duplicate_check=True` to bypass entirely (e.g.
    during a bulk migration). Soft matches surface as a warning without
    blocking.

    Args:
        title: Epic title (required, non-empty).
        description: Optional epic body / description.
        labels: Optional comma-separated label names.
        pipeline: Optional target pipeline.
        assignee: Optional GitHub username to assign at create time.
        estimate: Optional story-point estimate (numeric string).
        parent: Optional parent issue number; the new epic is wired as a
            sub-issue of that parent via addSubIssues.
        repo_path: Optional absolute path of a git checkout to run zh from.
        confirm_create: pass True to bypass the duplicate-check block.
        skip_duplicate_check: pass True to skip the pre-flight entirely.

    Returns:
        dict with: ok, number, epic_number (back-compat alias for number),
        url, type, pipeline, parent, estimate, estimate_requested,
        priority, priority_requested, raw, stderr, duplicate_check
        (when the pre-flight ran). v1.9.2 round-2 #4: the three-state
        estimate / priority splits documented for create_issue apply
        here too (compare *_requested against *). On block: ok=False,
        blocked=True, all key placeholders None / "" with
        duplicate_check populated.
    """
    return _with_epic_number_alias(_planning_create(
        "epic", title, description, labels, pipeline,
        assignee, estimate, parent, repo_path,
        confirm_create=confirm_create,
        skip_duplicate_check=skip_duplicate_check,
    ))


@mcp.tool()
def epic_update(epic_number: int, title: str = "", description: str = "",
                repo_path: str = "") -> dict:
    """Update an epic issue's title and/or description.

    At least one of `title` or `description` must be provided.

    Returns: dict with ok, number, epic_number (back-compat alias), raw, stderr.
    """
    return _with_epic_number_alias(_planning_update(
        "epic", epic_number, title, description, repo_path,
    ))


@mcp.tool()
def epic_add_children(epic_number: int, issue_numbers: list[int],
                      repo_path: str = "") -> dict:
    """Attach one or more issues as sub-issues of an epic (addSubIssues).

    Returns: dict with ok, partial_applied, parent, epic_number (back-compat
    alias for parent), added, added_requested, raw, stderr.
    v1.9.2 round-7 #10 / round-2 #5: partial_applied is True when the
    underlying cmd_subissue_add exited 2 (some children attached, others
    did not). `added` is the confirmed-landed list (== added_requested on
    full success; empty on partial — the bash wrapper cannot enumerate
    per-issue, consult subissue_list to verify). `added_requested` is
    the input list, always. The correct branching idiom is `if
    r["partial_applied"]: re-verify` first, then `elif r["ok"]: trust
    r["added"]`, else hard failure.
    """
    return _with_epic_number_alias(_planning_add_children(
        "epic", epic_number, issue_numbers, repo_path,
    ))


@mcp.tool()
def epic_remove_children(epic_number: int, issue_numbers: list[int],
                         repo_path: str = "") -> dict:
    """Detach one or more sub-issues from an epic (removeSubIssues).

    Returns: dict with ok, partial_applied, parent, epic_number (back-compat
    alias for parent), removed, removed_requested, raw, stderr.
    v1.9.2 round-7 #10 / round-2 #5: same partial_applied / requested
    split as the add path; `removed_requested` always echoes the input,
    `removed` is empty on partial (verify via subissue_list).
    """
    return _with_epic_number_alias(_planning_remove_children(
        "epic", epic_number, issue_numbers, repo_path,
    ))


@mcp.tool()
def epic_close(epic_number: int, comment: str = "", repo_path: str = "") -> dict:
    """Close an epic issue.

    DESTRUCTIVE: affects board visibility and notifies watchers. Pre-confirm.

    Returns: dict with ok, number, epic_number (back-compat alias), raw, stderr.
    """
    return _with_epic_number_alias(_planning_close(
        "epic", epic_number, comment, repo_path,
    ))


@mcp.tool()
def epic_reopen(epic_number: int, repo_path: str = "") -> dict:
    """Reopen a closed epic issue.

    Returns: dict with ok, number, epic_number (back-compat alias), raw, stderr.
    """
    return _with_epic_number_alias(_planning_reopen(
        "epic", epic_number, repo_path,
    ))


# ---- Initiative (level 1) ---------------------------------------------------
#
# Full surface (8 tools) parallel to epic_*. Each delegates to the same
# generic _planning_* helper, so adding behavior in one place updates every
# noun.

@mcp.tool()
def initiative_create(title: str, description: str = "", labels: str = "",
                      pipeline: str = "", assignee: str = "",
                      estimate: str = "", parent: int = 0,
                      repo_path: str = "",
                      confirm_create: bool = False,
                      skip_duplicate_check: bool = False) -> dict:
    """Create an Initiative (issue-type Initiative, level 1).

    v1.9.1 item #5: runs the same duplicate-check pre-flight as
    create_issue. Use confirm_create=True to override a block,
    skip_duplicate_check=True to bypass.

    Returns: dict with ok, number, url, type, pipeline, parent, estimate,
    estimate_requested, priority, priority_requested, raw, stderr,
    duplicate_check (when the pre-flight ran). v1.9.2 round-2 #4:
    the three-state estimate / priority splits documented for
    create_issue apply here too — compare estimate vs
    estimate_requested (null/null = not requested, N/N = applied,
    N/null = requested but setEstimate did not confirm; retry).
    """
    return _planning_create("initiative", title, description, labels,
                            pipeline, assignee, estimate, parent, repo_path,
                            confirm_create=confirm_create,
                            skip_duplicate_check=skip_duplicate_check)


@mcp.tool()
def initiative_list(repo_path: str = "") -> dict:
    """List issues of type Initiative. Returns ok, items, raw, stderr."""
    return _planning_list("initiative", repo_path)


@mcp.tool()
def initiative_show(number: int, repo_path: str = "") -> dict:
    """Show an Initiative issue + its child issues. Returns ok, number, raw."""
    return _planning_show("initiative", number, repo_path)


@mcp.tool()
def initiative_update(number: int, title: str = "", description: str = "",
                      repo_path: str = "") -> dict:
    """Update an Initiative's title and/or description.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_update("initiative", number, title, description,
                            repo_path)


@mcp.tool()
def initiative_add_children(number: int, issue_numbers: list[int],
                            repo_path: str = "") -> dict:
    """Attach issues (typically Projects/Epics) under an Initiative.

    Returns: dict with ok, partial_applied, parent, added, added_requested,
    raw, stderr. v1.9.2 round-7 #10 / round-2 #5: partial_applied=True
    means some children attached and some did not (cmd_subissue_add exit
    2); `added_requested` is the input list always, `added` is empty
    on partial (the wrapper cannot enumerate per-issue, verify via
    subissue_list).
    """
    return _planning_add_children("initiative", number, issue_numbers,
                                  repo_path)


@mcp.tool()
def initiative_remove_children(number: int, issue_numbers: list[int],
                               repo_path: str = "") -> dict:
    """Detach sub-issues from an Initiative.

    Returns: dict with ok, partial_applied, parent, removed, removed_requested,
    raw, stderr. v1.9.2 round-7 #10 / round-2 #5: same partial_applied /
    requested split as the add path.
    """
    return _planning_remove_children("initiative", number, issue_numbers,
                                     repo_path)


@mcp.tool()
def initiative_close(number: int, comment: str = "",
                     repo_path: str = "") -> dict:
    """Close an Initiative issue. DESTRUCTIVE.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_close("initiative", number, comment, repo_path)


@mcp.tool()
def initiative_reopen(number: int, repo_path: str = "") -> dict:
    """Reopen a closed Initiative issue.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_reopen("initiative", number, repo_path)


# ---- Project (level 2) ------------------------------------------------------

@mcp.tool()
def project_create(title: str, description: str = "", labels: str = "",
                   pipeline: str = "", assignee: str = "",
                   estimate: str = "", parent: int = 0,
                   repo_path: str = "",
                   confirm_create: bool = False,
                   skip_duplicate_check: bool = False) -> dict:
    """Create a Project (issue-type Project, level 2).

    v1.9.1 item #5: runs the same duplicate-check pre-flight as
    create_issue. Use confirm_create=True to override a block,
    skip_duplicate_check=True to bypass.

    Returns: dict with ok, number, url, type, pipeline, parent, estimate,
    estimate_requested, priority, priority_requested, raw, stderr,
    duplicate_check (when the pre-flight ran). v1.9.2 round-2 #4:
    the three-state estimate / priority splits documented for
    create_issue apply here too — compare estimate vs
    estimate_requested (null/null = not requested, N/N = applied,
    N/null = requested but setEstimate did not confirm; retry).
    """
    return _planning_create("project", title, description, labels, pipeline,
                            assignee, estimate, parent, repo_path,
                            confirm_create=confirm_create,
                            skip_duplicate_check=skip_duplicate_check)


@mcp.tool()
def project_list(repo_path: str = "") -> dict:
    """List issues of type Project. Returns ok, items, raw, stderr."""
    return _planning_list("project", repo_path)


@mcp.tool()
def project_show(number: int, repo_path: str = "") -> dict:
    """Show a Project issue + its child issues. Returns ok, number, raw."""
    return _planning_show("project", number, repo_path)


@mcp.tool()
def project_update(number: int, title: str = "", description: str = "",
                   repo_path: str = "") -> dict:
    """Update a Project's title and/or description.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_update("project", number, title, description, repo_path)


@mcp.tool()
def project_add_children(number: int, issue_numbers: list[int],
                         repo_path: str = "") -> dict:
    """Attach issues (typically Epics) under a Project.

    Returns: dict with ok, partial_applied, parent, added, added_requested,
    raw, stderr. v1.9.2 round-7 #10 / round-2 #5: partial_applied=True
    means some children attached and some did not (cmd_subissue_add exit
    2); `added_requested` is the input list always, `added` is empty
    on partial (the wrapper cannot enumerate per-issue, verify via
    subissue_list).
    """
    return _planning_add_children("project", number, issue_numbers, repo_path)


@mcp.tool()
def project_remove_children(number: int, issue_numbers: list[int],
                            repo_path: str = "") -> dict:
    """Detach sub-issues from a Project.

    Returns: dict with ok, partial_applied, parent, removed, removed_requested,
    raw, stderr. v1.9.2 round-7 #10 / round-2 #5: same partial_applied /
    requested split as the add path.
    """
    return _planning_remove_children("project", number, issue_numbers,
                                     repo_path)


@mcp.tool()
def project_close(number: int, comment: str = "",
                  repo_path: str = "") -> dict:
    """Close a Project issue. DESTRUCTIVE.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_close("project", number, comment, repo_path)


@mcp.tool()
def project_reopen(number: int, repo_path: str = "") -> dict:
    """Reopen a closed Project issue.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_reopen("project", number, repo_path)


# ---- Sub-task (level 5) -----------------------------------------------------

@mcp.tool()
def subtask_create(title: str, description: str = "", labels: str = "",
                   pipeline: str = "", assignee: str = "",
                   estimate: str = "", parent: int = 0,
                   repo_path: str = "",
                   confirm_create: bool = False,
                   skip_duplicate_check: bool = False) -> dict:
    """Create a Sub-task (issue-type Sub-task, level 5).

    v1.9.1 item #5: runs the same duplicate-check pre-flight as
    create_issue. Use confirm_create=True to override a block,
    skip_duplicate_check=True to bypass.

    Returns: dict with ok, number, url, type, pipeline, parent, estimate,
    estimate_requested, priority, priority_requested, raw, stderr,
    duplicate_check (when the pre-flight ran). v1.9.2 round-2 #4:
    the three-state estimate / priority splits documented for
    create_issue apply here too — compare estimate vs
    estimate_requested (null/null = not requested, N/N = applied,
    N/null = requested but setEstimate did not confirm; retry).
    """
    return _planning_create("subtask", title, description, labels, pipeline,
                            assignee, estimate, parent, repo_path,
                            confirm_create=confirm_create,
                            skip_duplicate_check=skip_duplicate_check)


@mcp.tool()
def subtask_list(repo_path: str = "") -> dict:
    """List issues of type Sub-task. Returns ok, items, raw, stderr."""
    return _planning_list("subtask", repo_path)


@mcp.tool()
def subtask_show(number: int, repo_path: str = "") -> dict:
    """Show a Sub-task issue + its child issues (if any).

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_show("subtask", number, repo_path)


@mcp.tool()
def subtask_update(number: int, title: str = "", description: str = "",
                   repo_path: str = "") -> dict:
    """Update a Sub-task's title and/or description.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_update("subtask", number, title, description, repo_path)


@mcp.tool()
def subtask_add_children(number: int, issue_numbers: list[int],
                         repo_path: str = "") -> dict:
    """Attach further sub-issues under a Sub-task.

    Returns: dict with ok, partial_applied, parent, added, added_requested,
    raw, stderr. v1.9.2 round-7 #10 / round-2 #5: partial_applied=True
    means some children attached and some did not (cmd_subissue_add exit
    2); `added_requested` is the input list always, `added` is empty
    on partial (the wrapper cannot enumerate per-issue, verify via
    subissue_list).
    """
    return _planning_add_children("subtask", number, issue_numbers, repo_path)


@mcp.tool()
def subtask_remove_children(number: int, issue_numbers: list[int],
                            repo_path: str = "") -> dict:
    """Detach sub-issues from a Sub-task.

    Returns: dict with ok, partial_applied, parent, removed, removed_requested,
    raw, stderr. v1.9.2 round-7 #10 / round-2 #5: same partial_applied /
    requested split as the add path.
    """
    return _planning_remove_children("subtask", number, issue_numbers,
                                     repo_path)


@mcp.tool()
def subtask_close(number: int, comment: str = "",
                  repo_path: str = "") -> dict:
    """Close a Sub-task issue. DESTRUCTIVE.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_close("subtask", number, comment, repo_path)


@mcp.tool()
def subtask_reopen(number: int, repo_path: str = "") -> dict:
    """Reopen a closed Sub-task issue.

    Returns: dict with ok, number, raw, stderr.
    """
    return _planning_reopen("subtask", number, repo_path)


# Note: *_delete is deliberately not exposed as an MCP tool for any planning
# level. Permanently deleting an issue is irreversible; do it via
# `zh delete <N>` directly from the CLI, which requires human deliberation.


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

    Calls ZenHub's GraphQL `githubChildIssues` connection directly from
    Python (no bash text contract). v1.9.0 migrated all sub-issue reads
    to githubChildIssues because that is the connection both
    addSubIssues and CreateIssueInput.parentIssueId populate in
    GitHub-backed workspaces (verified live, 2026-05-29);
    zenhubChildIssues stays empty for those writes. Walks pagination
    with the stuck-cursor + iteration-cap defenses carried over from
    the bash implementation. Each child dict carries its
    `repository.owner` and `.name` so callers can spot cross-repo
    children that can't be operated on from a single git checkout.

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
            total_count: int (the API's githubChildIssues.totalCount)
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
            ok: bool — true iff outcome in ("ok", "partial"). v1.9.2
                round-3 #2: aligned with _planning_add_children and
                set_issue_type so the same `addSubIssues` mutation
                yields the same `ok` semantic across both MCP
                surfaces. Branch on `partial_applied` first to
                distinguish full-success from partial-applied.
            partial_applied: bool — true iff outcome == "partial".
                Mirrors set_issue_type's signal: the mutation
                accepted on the ZenHub side but some inputs were
                not confirmed. Agents that retry on ok=False MUST
                check partial_applied first; retrying a partial
                produces duplicate adds for the children that DID
                land.
            parent_number: int
            outcome: "ok" | "partial" | "fail" | "noop"
            success_count: int — API-reported successCount
            failed_count: int — API-reported failedIssues length
            succeeded: list[int] — children the API actually linked
            failed: list[dict] — each {number, owner, name}
            unaccounted: list[int] — inputs the API did not report on
                in either succeeded or failed. Empty when the inferred
                succeeded set matches successCount (the trusted path).
                Populated under divergence (success_count != len(
                inferred_succeeded)) — succeeded is then empty and
                `unaccounted` exposes which input numbers the API
                neither confirmed nor explicitly failed. Pre-flight
                bails (parent-not-found, child-not-found) populate
                this with the un-attempted inputs so the conservation
                invariant holds across all return paths:
                len(succeeded) + len(failed) + len(unaccounted) ==
                len(deduped input child_numbers). Order preserves the
                deduped input order. (round-10 Pattern A)
            failed_unknown_count: int — count of `failedIssues`
                entries that lacked a usable issue `number` (null or
                non-int). Those entries bumped `failed_count` but
                are NOT in `failed` (no identifier to surface) and
                are NOT in `unaccounted` (the API DID report on them,
                just opaquely). When > 0, `partial_success_warning`
                names the count so the operator knows. (round-10
                Pattern A / round-9 #10)
            github_errors: dict | None
            partial_success_warning: str | None — set when the API's
                successCount diverges from the inferred succeeded
                set. Warning text is tailored by outcome shape
                (round-8 #1): "ok→partial" divergence reads
                "cannot identify which inputs succeeded"; strict
                noop divergence reads "strict no-op despite N
                input(s)"; under-reported fail reads "did not
                report on N input(s)". In every divergence case
                `succeeded` is empty because we can't identify
                which inputs landed. `outcome` is downgraded to
                "partial" only when it would otherwise have been
                "ok" — noop and fail keep their stronger semantics.
                Callers should re-list the parent's children to
                determine actual state.
            stderr: str
    """
    if not child_numbers:
        # Full result shape on the empty-input guard so strict MCP
        # callers don't KeyError on documented keys after a guard
        # rejection. Mirrors the sprint-tool fix from `bef3313`.
        return {
            "ok": False,
            "partial_applied": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "unaccounted": [],
            "failed_unknown_count": 0,
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": "child_numbers must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "partial_applied": False,
                "parent_number": parent_number, "outcome": "fail",
                "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [], "unaccounted": [],
                "failed_unknown_count": 0,
                "github_errors": None,
                "partial_success_warning": None}
    from zh_graphql_ops import add_sub_issues  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = add_sub_issues(ctx, parent_number, list(child_numbers))
    except ZhApiError as e:
        return {
            "ok": False,
            "partial_applied": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "unaccounted": [],
            "failed_unknown_count": 0,
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": str(e),
        }
    # v1.9.2 round-3 #2: align ok semantic with _planning_add_children
    # and set_issue_type — `ok` is True on both full success and
    # partial-applied so the same `addSubIssues` mutation does not
    # yield contradictory `ok` values across the two MCP surfaces
    # wrapping it. `partial_applied` carries the distinguishing
    # signal so an agent that retries on ok=False does not retry a
    # partial-success and double-attach the children that landed.
    outcome = result.get("outcome", "fail")
    partial_applied = outcome == "partial"
    return {
        "ok": outcome in ("ok", "partial"),
        "partial_applied": partial_applied,
        "parent_number": parent_number,
        "outcome": outcome,
        "success_count": result.get("success_count", 0),
        "failed_count": result.get("failed_count", 0),
        "succeeded": result.get("succeeded", []),
        "failed": result.get("failed", []),
        "unaccounted": result.get("unaccounted", []),
        "failed_unknown_count": result.get("failed_unknown_count", 0),
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
            ok: bool — true iff outcome in ("ok", "partial"). v1.9.2
                round-3 #2: aligned with subissue_add_children's
                semantic and the planning-children wrappers.
            partial_applied: bool — true iff outcome == "partial".
                Branch on this BEFORE ok to detect partials.
            parent_number: int
            outcome: "ok" | "partial" | "fail" | "noop"
            success_count: int
            failed_count: int
            succeeded: list[int]
            failed: list[dict]
            unaccounted: list[int] — inputs the API did not report on
                in either succeeded or failed. Empty on the trusted
                path; populated under divergence OR under pre-flight
                bails (parent-not-found, validation-failed). Order
                preserves deduped input order. Conservation invariant:
                len(succeeded) + len(failed) + len(unaccounted) ==
                len(deduped input child_numbers). (round-10 Pattern A)
            failed_unknown_count: int — see subissue_add_children.
            github_errors: dict | None
            partial_success_warning: str | None — set when the API's
                successCount diverges from the inferred succeeded
                set. Warning text is tailored by outcome shape
                (round-8 #1) — see subissue_add_children for the
                three variants. `succeeded` is empty under
                divergence in all cases. `outcome` is downgraded to
                "partial" only when it would otherwise have been
                "ok" (round-7 #1 made the `add` and `remove`
                guards match); `noop` and `fail` keep their
                stronger semantics. Callers should re-list the
                parent's children to determine actual state.
            stderr: str
    """
    if not child_numbers:
        # Full result shape on the empty-input guard so strict MCP
        # callers don't KeyError on documented keys after a guard
        # rejection. Mirrors the sprint-tool fix from `bef3313`.
        return {
            "ok": False,
            "partial_applied": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "unaccounted": [],
            "failed_unknown_count": 0,
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": "child_numbers must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "partial_applied": False,
                "parent_number": parent_number, "outcome": "fail",
                "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [], "unaccounted": [],
                "failed_unknown_count": 0,
                "github_errors": None,
                "partial_success_warning": None}
    from zh_graphql_ops import remove_sub_issues  # noqa: PLC0415
    from zh_api import ZhApiError  # noqa: PLC0415
    try:
        result = remove_sub_issues(ctx, parent_number, list(child_numbers))
    except ZhApiError as e:
        return {
            "ok": False,
            "partial_applied": False,
            "parent_number": parent_number,
            "outcome": "fail",
            "success_count": 0,
            "failed_count": 0,
            "succeeded": [],
            "failed": [],
            "unaccounted": [],
            "failed_unknown_count": 0,
            "github_errors": None,
            "partial_success_warning": None,
            "stderr": str(e),
        }
    # v1.9.2 round-3 #2: align ok semantic — see subissue_add_children.
    outcome = result.get("outcome", "fail")
    partial_applied = outcome == "partial"
    return {
        "ok": outcome in ("ok", "partial"),
        "partial_applied": partial_applied,
        "parent_number": parent_number,
        "outcome": outcome,
        "success_count": result.get("success_count", 0),
        "failed_count": result.get("failed_count", 0),
        "succeeded": result.get("succeeded", []),
        "failed": result.get("failed", []),
        "unaccounted": result.get("unaccounted", []),
        "failed_unknown_count": result.get("failed_unknown_count", 0),
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
    anchoring (top/bottom/after/before) is computed in Python from the
    `githubChildIssues` listing (the canonical sub-issue connection in
    GitHub-backed workspaces; see subissue_list for the rationale).

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
            unaccounted: list[int] — canonical mutation-tool key
                (round-10 Pattern A). Empty on the trusted path
                (succeeded + failed exhaustively partition the input
                set). Populated on pre-flight bails (sprint-not-found,
                issue-not-found) with the un-attempted inputs so the
                conservation invariant holds:
                    len(succeeded) + len(failed) + len(unaccounted)
                        == len(deduped input issue_numbers).
            partial_success_warning: str | None — canonical
                mutation-tool key. Currently always None for
                add_issues_to_sprint (no divergence-detection
                surface here); reserved for future use.
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
            "unaccounted": [],
            "partial_success_warning": None,
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
            "unaccounted": [],
            "partial_success_warning": None,
            "stderr": "sprint_name must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "sprint_id": None, "sprint_name": sprint_name,
                "outcome": "fail", "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [],
                "unaccounted": [], "partial_success_warning": None}
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
            "unaccounted": [],
            "partial_success_warning": None,
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
        "unaccounted": result.get("unaccounted", []),
        "partial_success_warning": result.get("partial_success_warning"),
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
        response omitted or returned an empty `sprints` array, OR
        when the post-state walk only covered part of the sprint
        (in which case `response_anomaly` is extended with a
        coverage note pointing at a re-verification command).

    Coverage semantics: when `inspected_full=False` the outcome is
    DOWNGRADED to `partial` (or `fail` when zero positives
    confirmed). `succeeded` lists ONLY inputs the partial walk
    actually observed AND observed as absent from the post-state
    (round-5 #1: previously inputs the walker never reached were
    incorrectly counted as succeeded). `failed` lists inputs the
    walker observed still-attached. Inputs the walker never
    reached are NEITHER succeeded NOR failed — they're un-verified,
    surfaced in BOTH the `unaccounted` structured field (round-10
    Pattern A / round-9 #6) AND `response_anomaly` text, with the
    text's count derived from the field (no arithmetic drift).
    Re-verify with `zh sprint show '<name>'`.

    The conservation invariant holds across every return path:
        len(succeeded) + len(failed) + len(unaccounted)
            == len(deduped input issue_numbers)
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
            "unaccounted": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "partial_success_warning": None,
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
            "unaccounted": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "partial_success_warning": None,
            "stderr": "sprint_name must be non-empty",
        }
    ctx, err = _resolve_ctx(repo_path)
    if err is not None:
        return {**err, "sprint_id": None, "sprint_name": sprint_name,
                "outcome": "fail", "success_count": 0, "failed_count": 0,
                "succeeded": [], "failed": [],
                "unaccounted": [],
                "inspected_full": False,
                "pagination_warning": None,
                "response_anomaly": None,
                "partial_success_warning": None}
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
            "unaccounted": [],
            "inspected_full": False,
            "pagination_warning": None,
            "response_anomaly": None,
            "partial_success_warning": None,
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
        "unaccounted": result.get("unaccounted", []),
        "inspected_full": result.get("inspected_full", False),
        "pagination_warning": result.get("pagination_warning"),
        "response_anomaly": result.get("response_anomaly"),
        "partial_success_warning": result.get("partial_success_warning"),
        "stderr": result.get("error") or "",
    }


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    mcp.run()
