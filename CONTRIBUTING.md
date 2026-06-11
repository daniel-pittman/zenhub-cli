# Contributing to `zh` (ZenHub CLI)

Thanks for your interest. This project is small and bash-script-driven; the contribution workflow is correspondingly lightweight.

## Filing issues

Open a regular [GitHub issue](https://github.com/daniel-pittman/zenhub-cli/issues) for anything except security reports — bug reports, feature requests, doc fixes, questions about behaviour.

**Security issues** go through [`SECURITY.md`](SECURITY.md), not the public tracker. See its "Reporting a vulnerability" section.

## Pull request workflow

1. Fork the repo and create a feature branch off `develop` (the default branch).
2. Make your change. The required `syntax` CI check runs three things and they must all be clean:
   - `bash -n zh`
   - `python -m py_compile mcp_server.py zh_api.py zh_graphql_ops.py similarity.py`
   - `python -m pytest tests/`
3. Open the PR against `develop`. The `main` branch only receives release PRs from `develop` (it's the stable line that the tags point at).
4. Address review feedback. Once approved and CI passes, a maintainer will merge.

The first time an outside contributor opens a PR, GitHub holds Actions execution pending maintainer approval — this is the "Require approval for all outside collaborators" gate documented in [`SECURITY.md` §1](SECURITY.md). The PR is fine; it just may sit briefly before workflows start.

## CI and review automation

- **`syntax` (required)** — bash and Python syntax checks plus the pytest suite across Python 3.10/3.11/3.12. Must pass before merge into `develop` or `main`. No secrets needed; runs on every push and PR including from forks.
- **Claude code review** — automated PR review using a subscription-bound OAuth token. Output appears as a PR comment, and the review now emits a machine-readable verdict that the `review-gate` check enforces (see below).
- **Semgrep security scan (advisory)**: free, token-free Semgrep OSS (`p/python`, `p/bash`, `p/secrets`, `p/ci`) runs first on every PR and posts a single sticky findings comment, which the Claude code review folds into its analysis. No API key; advisory.

### Merge gate (`review-gate`)

The Claude review ends with a machine-readable verdict (`<!-- REVIEW-GATE verdict=PASS|BLOCK blocking=N -->`) and the `review-gate` check turns it into a real signal:

- **PASS** — no HIGH/CRITICAL findings. Check green.
- **BLOCK** — at least one HIGH/CRITICAL finding (a correctness bug on a common path, a security/data-loss issue, a build/release breakage, or a CI Tests failure). Push a fix — the review re-runs on every push and can clear it — or a maintainer overrides (below). Style, missing-tests double-counting, and speculative hardening deliberately do **not** block.
- **INCONCLUSIVE** — the review didn't complete (usually a transient API throttle) or emitted no parseable verdict. Distinct, re-runnable, and **also red**: a flaky review never reads as a clean pass. Re-run via Actions → Claude Code Review → Run workflow (with the PR number), or override.

**Override:** a maintainer (write/maintain/admin) applies the `review-ack` label. It's honored only when applied at or after the current head commit, so a stale ack can't clear a finding on newly-pushed code. Admin branch-protection bypass remains the emergency break-glass.

**Note for PRs that edit `.github/workflows/claude-code-review.yml` itself:** the review action refuses to run on a PR that modifies its own workflow file (a token-exfiltration guard — "Workflow validation failed … this is normal"). Such a PR will show the review as failed and the gate as INCONCLUSIVE; that's expected. Merge it through maintainer review of the non-review checks (`syntax`, `semgrep`) plus the diff; subsequent PRs are reviewed normally.

The interactive `@claude` bot is available for maintainer-triggered triage. **Only comments authored by `OWNER`, `MEMBER`, or `COLLABORATOR` accounts trigger it**, by design — outside contributors who type `@claude` won't get a response, to bound subscription-quota burn. See [`SECURITY.md` §5](SECURITY.md) for the rationale.

## Code style

- `zh` follows the existing `cmd_*` function pattern. New subcommands should mirror the closest existing analogue (`cmd_block` for a single mutation, `cmd_epic_add` for a multi-arg batched mutation, etc.).
- The MCP server's GraphQL-direct surface lives in `zh_api.py` (config / auth / repo+workspace resolution / transport) and `zh_graphql_ops.py` (queries and mutations). New MCP tools that need direct GraphQL access should follow that split — keep query strings + business logic in `zh_graphql_ops`, route every API call through `RepoContext.query` so the unit tests can mock at one place.
- Personal identifiers (real ticket numbers, org names, paths) don't belong in the repo. The agent file at `agents/zenhub.md` is a deliberately generic template.
- Updates to commands should be reflected in `README.md`, `CLAUDE.md`, and `agents/zenhub.md` together — they all describe the same surface from different angles.

## Running the tests locally

Run the suite in a dedicated virtualenv, not your global interpreter:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

The tests mock the network entirely (no live ZenHub or GitHub calls), so they're fast and don't need credentials. `requirements-dev.txt` is the single declared source for the test dependencies (pytest + numpy); CI installs from the same file, so a local venv run matches CI exactly.

**Why a venv matters here:** the suite imports its fixtures through the `tests` namespace package (`tests/` has no `__init__.py`). If a *different* project that ships a regular `tests` package is editable-installed (`pip install -e`) into the interpreter you run pytest with, that regular package shadows this one and `tests._fixtures` fails to import (a regular package outranks a namespace package on `sys.path`). A clean `.venv` per the commands above is immune to whatever is installed globally. This is also exactly how CI stays reliable: it runs in a fresh, isolated environment every time.
