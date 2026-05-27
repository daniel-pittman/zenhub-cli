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
  ZH_MCP_VENV          — full path of the venv directory to use; overrides
                         the XDG_DATA_HOME-derived default. Useful for
                         pinning to a project-local venv during development.
  XDG_DATA_HOME        — standard XDG override for the data root; the venv
                         is created at `$XDG_DATA_HOME/zh/venv`.
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
import time
from contextlib import contextmanager
from pathlib import Path

# -----------------------------------------------------------------------------
# Self-bootstrap: build (or rebuild, if broken / stale) a durable venv under
# XDG_DATA_HOME, validate it, and re-exec under it. Must run before any
# third-party import (mcp).
# -----------------------------------------------------------------------------


def _default_venv_dir() -> Path:
    """Compute the default venv location.

    Priority:
      1. ZH_MCP_VENV environment variable (full path).
      2. $XDG_DATA_HOME/zh/venv (standard XDG; defaults to
         ~/.local/share/zh/venv when XDG_DATA_HOME is unset).
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
        return Path(override).expanduser().resolve()
    xdg_raw = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    xdg_data = os.path.expanduser(xdg_raw)
    if xdg_data.startswith("~"):
        # `expanduser` returns the input unchanged when HOME is unset
        # (sandboxed CI, certain systemd / launchd configurations).
        # `Path("~/...").resolve()` would then resolve against cwd and
        # produce a literal `~` directory under the working directory —
        # different across launches, triggering rebuild loops.
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
    return (Path(xdg_data) / "zh" / "venv").resolve()


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

# Subprocess timeouts. The per-launch probe stays snappy (mcp only) so
# slow cold-disk transformers imports don't trigger needless rebuild
# loops; the post-build probe runs once when caches are warm anyway.
_VENV_PER_LAUNCH_PROBE_TIMEOUT = 15   # `import mcp` only — fast
_VENV_FULL_PROBE_TIMEOUT = 60         # all _VENV_DEPS — cold-cache torch import can take ~30s
_VENV_BUILD_TIMEOUT = 60              # `python -m venv ...`
_VENV_PIP_TIMEOUT = 600               # `pip install ...` (torch is ~400MB)


def _venv_per_launch_probe() -> str:
    """Lightweight probe — just `import mcp` — runs on every MCP launch.

    Importing the full _VENV_DEPS tuple (sentence_transformers + torch +
    numpy) takes seconds on a warm cache and tens of seconds on a cold
    one — far too costly to pay on every launch. The post-build probe
    (`_venv_full_probe`) catches half-installed venvs once; per-launch
    just confirms the interpreter still runs and mcp still imports.
    """
    return "import mcp"


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


def _ensure_safe_parent(venv_dir: Path) -> None:
    """Validate the path's parent + grandparent before any mkdir.

    Guards against two failure modes:
      1. A typo'd `ZH_MCP_VENV=~/something/zh-venv` (when `~/something`
         doesn't exist) silently materializing a 500MB venv tree at
         an unexpected location. `mkdir(parents=True, ...)` would do
         this without complaint; we require the grandparent to exist
         and only create one new directory level.
      2. A pre-existing regular file at the parent path (typo'd
         earlier `mkdir -p ~/.local/share && touch ~/.local/share/zh`,
         or `ZH_MCP_VENV=~/.bashrc/whatever`) producing an unhelpful
         `FileExistsError: [Errno 17]` traceback. We raise a clear
         RuntimeError naming the offending path instead.
    """
    parent = venv_dir.parent
    if parent.exists():
        if not parent.is_dir():
            raise RuntimeError(
                f"[zenhub-mcp] refusing to bootstrap into {venv_dir}: "
                f"parent {parent} exists but is not a directory. Remove "
                f"or rename the stray file, or set ZH_MCP_VENV / "
                f"XDG_DATA_HOME to point elsewhere."
            )
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
    parent.mkdir()  # single new level only


def _safe_rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    """Robust rmtree that handles symlinks + EACCES gracefully.

    Plain `shutil.rmtree` raises on symlinked dirs (`OSError: Cannot
    call rmtree on a symbolic link`) and on permission errors mid-walk
    (e.g. a user-owned `__pycache__/` with mode 000 from an aborted
    install, or a parent dir whose write+execute bits were dropped
    by another tool). This wrapper:
      - unlinks symlinks instead of recursing (preserves the target)
      - on `PermissionError` during a walk, chmods BOTH the failing
        path AND its parent (unlink permission is parent-driven on
        POSIX, not file-mode-driven) and retries
      - re-raises if a retry still fails, unless `ignore_errors=True`

    NOTE: a root-owned file the current user can't `chmod` is not
    recoverable here (the chmod itself raises `PermissionError`); we
    fall through and re-raise. The caller in that case must clean up
    via `sudo rm -rf` manually. Cleanup-path callers (inside
    `_build_venv` except blocks) pass `ignore_errors=True` so a
    failed cleanup doesn't mask the original failure.
    """
    try:
        if path.is_symlink():
            # Don't follow the symlink and rmtree the target! Just
            # remove the link itself.
            path.unlink()
            return

        def _on_error(func, p, exc_info):
            # `onerror` (not `onexc`) for Python 3.10 / 3.11 compat.
            exc_type = exc_info[0]
            # Defensive `is not None` guard: some `shutil.rmtree`
            # internals can pass `(None, ..., ...)` for non-OSError
            # cases, and `issubclass(None, ...)` would TypeError-mask
            # the real exception.
            if exc_type is not None and issubclass(exc_type, PermissionError):
                # Permission to unlink depends on the PARENT dir's
                # write+execute bits, not on the file's mode. Try
                # chmod-ing the parent first, then the file itself
                # (in case it's a sub-dir we need to recurse into).
                try:
                    os.chmod(os.path.dirname(p), 0o700)
                except OSError:
                    pass
                try:
                    os.chmod(p, 0o700)
                    func(p)
                    return
                except OSError:
                    pass
            raise exc_info[1]

        shutil.rmtree(path, onerror=_on_error)
    except OSError:
        if not ignore_errors:
            raise


def _build_venv(venv_dir: Path, deps_hash: str) -> None:
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
    _ensure_safe_parent(venv_dir)
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
        sentinel.write_text(
            f"deps_hash={deps_hash}\nstarted_at={time.time()}\n",
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
    # Atomic marker write: write to tmp, then os.replace. A SIGKILL
    # between probe success and the marker landing would otherwise
    # leave a working venv that fails validity checks forever.
    marker = venv_dir / _VENV_MARKER
    tmp_marker = marker.with_suffix(".tmp")
    tmp_marker.write_text(deps_hash, encoding="utf-8")
    os.replace(tmp_marker, marker)
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
def _venv_build_lock(venv_dir: Path):
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
    `venv_dir.parent / _BOOTSTRAP_LOCK` — guaranteeing the parent
    exists as a directory is the safety guarantee that `_build_venv`
    later depends on.
    """
    _ensure_safe_parent(venv_dir)
    lock_path = venv_dir.parent / _BOOTSTRAP_LOCK
    # Open with O_CREAT so the lock file appears the first time; keep
    # it open across the lock window so the fd stays valid.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
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
    venv_dir = _default_venv_dir()
    venv_py = venv_dir / "bin" / "python3"
    deps_hash = _deps_hash()
    if not _venv_is_valid(venv_dir, deps_hash):
        # Serialize concurrent bootstraps. After acquiring the lock,
        # re-check validity — another process may have finished while
        # we were waiting, in which case skipping the rebuild saves
        # ~5 minutes of redundant torch download.
        with _venv_build_lock(venv_dir):
            if not _venv_is_valid(venv_dir, deps_hash):
                _build_venv(venv_dir, deps_hash)
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
            with _venv_build_lock(venv_dir):
                if not _venv_is_valid(venv_dir, deps_hash):
                    _build_venv(venv_dir, deps_hash)
            sys.stderr.flush()
            sys.stdout.flush()
            os.execv(str(venv_py), argv)


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
# `  #645 │ owner/repo │ ...` (2-space indent). Bounded to 0-3 leading
# spaces/tabs so a 4-space-indented title line that legitimately starts
# with `#NNN │` (a cross-reference convention some teams use, e.g.
# `    #1234 │ blocker note for OAuth retry path`) isn't mistaken for
# the next issue's header. Tab support is defensive — `zh` currently
# emits spaces, but if a future formatting pass switches to tabs the
# parser should degrade gracefully (skip the line) instead of mis-parse.
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


# `zh` emits the success line with a ✓ prefix (after ANSI is stripped):
#   ✓ Created issue #42: <title>
# An unanchored search across stdout would also match titles that
# legitimately contain "Created issue #NN" (e.g. a bug report whose
# title quotes an earlier ticket), or the preceding `Info: Creating
# issue: <title>...` line. Anchor at line-start + ✓ + ws so the
# captured number is the one immediately after the ✓ marker.
# `[ \t]*` (not `\s*`) so the leading-whitespace match can't traverse
# newlines and accidentally span multiple lines — defensive pin on the
# "match a single line starting with ✓" contract.
_SUCCESS_ISSUE_RE = re.compile(
    r"^[ \t]*✓[ \t]*Created issue #(\d+)", re.MULTILINE,
)
_SUCCESS_EPIC_RE = re.compile(
    r"^[ \t]*✓[ \t]*Created epic #(\d+)", re.MULTILINE,
)


def _parse_new_issue_number(plain: str) -> int | None:
    """Extract issue number from the `✓ Created issue #NNN` success line."""
    m = _SUCCESS_ISSUE_RE.search(plain)
    return int(m.group(1)) if m else None


def _parse_new_epic_number(plain: str) -> int | None:
    """Extract epic number from the `✓ Created epic #NNN` success line."""
    m = _SUCCESS_EPIC_RE.search(plain)
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
        return {**err, "parent_number": parent_number, "outcome": "fail",
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
    return {
        "ok": result.get("ok", False),
        "parent_number": parent_number,
        "outcome": result.get("outcome", "fail"),
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
            ok: bool — true iff outcome == "ok"
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
        return {**err, "parent_number": parent_number, "outcome": "fail",
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
    return {
        "ok": result.get("ok", False),
        "parent_number": parent_number,
        "outcome": result.get("outcome", "fail"),
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
