"""Test harness for running the REAL `zh` script under test.

The historical regression tests in `test_zh_bash_regression.py` embedded
parallel `_SNIPPET` strings that re-implemented production bash. The
snippet author had to keep the snippet byte-identical to production;
when production drifted, the snippet passed against itself and the
production bug shipped. The v1.9.1 round-4 finding ("envelope stub
returns 1 while production exits 1, snippet's `if cmd; then else` form
catches return-1 but production's exit terminates the shell") was a
textbook instance: the test passed against the snippet, but the same
behavior in production aborted the script. Six review rounds did not
catch it because every reviewer read the snippet, not production.

The fix is structural. v1.9.2 makes the `zh` bash script sourceable
(via a guarded `main "$@"` at the bottom) and this helper provides a
small harness for tests to:

  1. Source `zh` so every production `cmd_*` function is available.
  2. Optionally inject stub overrides for I/O helpers (`zh_graphql`,
     `gh`, `get_repo_info`, etc.) so tests can drive a real `cmd_*`
     with controlled inputs.
  3. Capture stdout, stderr, and exit code.

Stub functions are defined AFTER `source zh`, so they override
production's same-named functions — bash function definitions are
overwritten by later same-name definitions in the same shell. The
production `cmd_*` function then calls the stub, not the real
network-facing helper.

This is the canonical pattern for tests added from v1.9.2 onward.
Pre-v1.9.2 snippet tests remain in `test_zh_bash_regression.py` (they
still provide coverage and most of their drift risk is on jq
projections that have been stable for many rounds), but new tests
should target production via this runner.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
from pathlib import Path

# Repo root resolves through whatever cwd the test is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
ZH_SCRIPT = REPO_ROOT / "zh"

# v1.9.2 round-4 (PR #27) finding #11: track every isolated-HOME
# tempdir the harness creates and remove them all at process exit.
# Each `run_zh_with_stubs` call provisions a fresh tempdir to defeat
# stray `/tmp/.config/zh/config` interference (round-3 #10); over a
# full ~50-test cycle that's dozens of dirs left in TMPDIR for local
# devs. CI runners wipe scratch between jobs so this didn't matter
# there, but local-iteration tempdirs accumulate without a sweeper.
_HARNESS_TEMPDIRS: list[str] = []


def _cleanup_harness_tempdirs() -> None:
    """atexit hook: remove every tempdir provisioned by the harness.

    `ignore_errors=True` so a dir already removed by a prior cleanup,
    a file the test process locked, or a permission glitch on shared
    CI tmpfs cannot fail the test suite at exit.
    """
    for path in _HARNESS_TEMPDIRS:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_harness_tempdirs)


def run_zh_with_stubs(
    stubs: str,
    invocation: str,
    *,
    args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    stdin: str | None = None,
    cwd: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Source the real `zh` script, install stubs, then run `invocation`.

    The wrapper script that bash runs has three stages:

      1. Set required env vars so `load_config` and similar helpers do
         not try to touch real config / network.
      2. `source` the real `zh` script. Every production function is
         now defined in the current shell.
      3. `eval` the caller's `stubs` block. Stubs OVERRIDE same-named
         production functions because the latest `function name() {}`
         definition wins.
      4. `eval` the caller's `invocation` block. This is normally a
         single call to a production `cmd_*` function.

    Args:
        stubs: bash snippet defining stub functions (e.g.
            `zh_graphql() { printf '%s' "$STUB_RESPONSE"; }`). Can
            also set local shell vars the stubs read from.
        invocation: bash snippet that calls the production function(s)
            under test. Receives `"$@"` from the `args` list. Examples:
            `cmd_create "$@"`, `cmd_set_type 42 Epic`.
        args: positional args passed as `$1`, `$2`, ... to the
            invocation block.
        extra_env: additional env vars (overrides the defaults the
            harness sets).
        stdin: optional stdin text for the bash process.
        cwd: optional working directory; defaults to a temporary-ish
            location (the repo root, so `./zh` paths resolve).
        timeout: subprocess timeout in seconds.

    Returns:
        subprocess.CompletedProcess (returncode, stdout, stderr).
    """
    # Defaults that keep load_config and the helpers from reaching out:
    # ZH_TOKEN must be set or zh_graphql refuses to run.
    #
    # v1.9.2 round-3 (PR #27) finding #10: HOME points at an isolated
    # per-call tempdir instead of /tmp so a stray /tmp/.config/zh/config
    # (from another tenant on a shared CI host, or a prior test run)
    # cannot silently override the harness-provided ZH_TOKEN / ZH_REPO
    # / ZH_WORKSPACE via load_config's source step. The tempdir is
    # left in place for inspection if a test fails (Python's GC will
    # not auto-remove it); modern CI runners wipe scratch between
    # jobs, so this does not leak across runs.
    import tempfile
    _isolated_home = tempfile.mkdtemp(prefix="zh-test-home-")
    # v1.9.2 round-4 (PR #27) finding #11: register for atexit cleanup
    # so local-iteration runs don't accumulate dozens of tempdirs.
    _HARNESS_TEMPDIRS.append(_isolated_home)
    env_defaults = {
        # PATH must include common locations so jq/gh/curl resolve if
        # tests actually invoke them (they shouldn't, but the harness
        # should not break path-sensitive helpers).
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "ZH_TOKEN": "test-token-do-not-use",
        "ZH_REPO": "acme/widgets",
        "ZH_WORKSPACE": "TestWS",
        # Per-call isolated HOME so load_config cannot source a
        # stray config file written by another process / tenant.
        "HOME": _isolated_home,
        # Honor the NO_COLOR informal standard. As of round-3 #9,
        # `zh` now respects this and suppresses ANSI escapes when
        # set, so tests can match plain text without strip helpers
        # in NEW assertions (existing helpers stay for back-compat).
        "NO_COLOR": "1",
    }
    if extra_env:
        env_defaults.update(extra_env)

    # The wrapper sources the actual zh script. Use the absolute path
    # so the cwd doesn't matter (and so tests run from any directory).
    #
    # IMPORTANT: do NOT disable `set -e` after source. Production runs
    # with `set -euo pipefail`; disabling it in the harness would hide
    # exactly the round-4 / round-7 #5 class of bug (bare command
    # substitution that aborts under set -e). The harness lets the
    # production safety flags stay armed so a non-fail-soft envelope
    # in production fails the test loudly.
    #
    # Stubs are applied AFTER source; bash function redefinitions
    # override earlier ones in the same shell.
    wrapper = (
        f'source "{ZH_SCRIPT}"\n'
        f"{stubs}\n"
        f"{invocation}\n"
    )

    cmd = ["bash", "-c", wrapper, "_"]
    if args:
        cmd.extend(args)

    # v1.9.2 round-3 (PR #27) finding #7: inherit specific parent-env
    # vars so locale (LANG / LC_*), TMPDIR, TERM, USER,
    # PYTHONIOENCODING and similar harness-friendly vars survive.
    #
    # v1.9.2 round-4 (PR #27) finding #4: use an ALLOWLIST instead of
    # `dict(os.environ, **env_defaults)`. The earlier merge passed
    # through every `ZH_*` var from the developer's shell — most
    # consequentially ZH_REST_TOKEN, which a test that ran a
    # REST-using code path without stubbing the REST helper would
    # send to the live ZenHub API along with the developer's real
    # credential. The allowlist restricts inheritance to a curated
    # set of environment-shaping vars (locale / tmpdir / etc.) that
    # affect rendering but carry no secrets, then layers
    # env_defaults on top so the harness's explicit overrides win.
    import os as _os
    _ALLOWED_INHERIT = (
        "LANG", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_TIME",
        "LC_NUMERIC", "LC_MESSAGES",
        "TMPDIR", "TERM", "USER", "LOGNAME", "SHELL",
        "PYTHONIOENCODING",
    )
    inherited = {k: _os.environ[k] for k in _ALLOWED_INHERIT if k in _os.environ}
    merged_env = {**inherited, **env_defaults}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=merged_env,
        input=stdin,
        cwd=cwd or str(REPO_ROOT),
        timeout=timeout,
        check=False,
    )


def run_zh_function(
    func_name: str,
    args: list[str],
    *,
    stubs: str = "",
    extra_env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """Convenience wrapper: call a production function with args.

    Equivalent to `run_zh_with_stubs(stubs, f'{func_name} "$@"', args=args)`.
    Use this when the test only needs to invoke one `cmd_*` and doesn't
    need any inline pre/post bash.
    """
    return run_zh_with_stubs(
        stubs=stubs,
        invocation=f'{func_name} "$@"',
        args=args,
        extra_env=extra_env,
        stdin=stdin,
    )
