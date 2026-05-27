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

- **`syntax` (required)** — bash and Python syntax checks. Must pass before merge into `develop` or `main`. No secrets needed; runs on every push and PR including from forks.
- **Claude code review (advisory)** — automated PR review using a subscription-bound OAuth token. Output appears as a PR comment; not a merge gate.
- **Claude security review (advisory)** — runs only on PRs targeting `main` or `develop`. Uses the metered Anthropic API key; also advisory.

The interactive `@claude` bot is available for maintainer-triggered triage. **Only comments authored by `OWNER`, `MEMBER`, or `COLLABORATOR` accounts trigger it**, by design — outside contributors who type `@claude` won't get a response, to bound subscription-quota burn. See [`SECURITY.md` §5](SECURITY.md) for the rationale.

## Code style

- `zh` follows the existing `cmd_*` function pattern. New subcommands should mirror the closest existing analogue (`cmd_block` for a single mutation, `cmd_epic_add` for a multi-arg batched mutation, etc.).
- The MCP server's GraphQL-direct surface lives in `zh_api.py` (config / auth / repo+workspace resolution / transport) and `zh_graphql_ops.py` (queries and mutations). New MCP tools that need direct GraphQL access should follow that split — keep query strings + business logic in `zh_graphql_ops`, route every API call through `RepoContext.query` so the unit tests can mock at one place.
- Personal identifiers (real ticket numbers, org names, paths) don't belong in the repo. The agent file at `agents/zenhub.md` is a deliberately generic template.
- Updates to commands should be reflected in `README.md`, `CLAUDE.md`, and `agents/zenhub.md` together — they all describe the same surface from different angles.

## Running tests locally

```bash
python -m pip install pytest
python -m pytest tests/ -v
```

The tests mock the network entirely (no live ZenHub or GitHub calls), so they're fast and don't need credentials.
