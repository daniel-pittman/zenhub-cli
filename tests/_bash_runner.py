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

import subprocess
from pathlib import Path

# Repo root resolves through whatever cwd the test is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
ZH_SCRIPT = REPO_ROOT / "zh"


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
    env_defaults = {
        # PATH must include common locations so jq/gh/curl resolve if
        # tests actually invoke them (they shouldn't, but the harness
        # should not break path-sensitive helpers).
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "ZH_TOKEN": "test-token-do-not-use",
        "ZH_REPO": "acme/widgets",
        "ZH_WORKSPACE": "TestWS",
        # Keep config-file probing disabled by pointing at /tmp where
        # there is no zh config; load_config tolerates this.
        "HOME": "/tmp",
        # Force-disable color so success / warn output is plain text
        # the tests can match.
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

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env_defaults,
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
