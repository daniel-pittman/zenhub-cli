# CLAUDE.md

This file provides guidance to Claude Code and other AI assistants when working with the ZenHub CLI tool.

## Project Overview

`zh` is a bash-based command-line interface for ZenHub that wraps both the GraphQL and REST APIs. It's designed to be used directly by developers or by AI assistants helping with project management tasks.

## Quick Reference

### Common Commands

```bash
# View issues and board
zh issue <number>       # View issue details (shows ZenHub + GitHub URLs)
zh mine                 # Your assigned issues (with ZenHub URLs)
zh mine @username       # Issues assigned to user
zh mine --no-urls       # Compact output without URLs
zh users                # List assignable users
zh board                # Board overview
zh pipeline "Name"      # Issues in a pipeline (with ZenHub URLs)

# Manage issues
zh move <issue> "Pipeline"    # Move to pipeline
zh reorder <issue> top        # Prioritize
zh estimate <issue> <points>  # Set estimate
zh assign <issue> <user>      # Assign user
zh unassign <issue> [user]    # Remove assignee(s)
zh comment <issue> "text"     # Add comment
zh close <issue> [comment]    # Close issue
zh reopen <issue>             # Reopen closed issue
zh delete <issue> [-y]        # DANGER: permanently delete a GitHub issue (via gh; prefer close). Prompts when interactive; -y skips

# Create issues
zh create "Title" -t Bug -l "label1,label2" -e 3 -p "TO DO"
zh create "Title" -t Bug --priority "High"    # Set a workspace priority at create time
zh create "Title" -t Epic --json              # Machine-readable JSON on stdout (batch callers)
zh create "Title" -t Feature --parent 42 -q   # Nest under #42; emit only the new number
zh type <issue> <name>                        # Change an existing issue's type

# Dependencies
zh block <blocked> <blocker>  # Set dependency
zh unblock <blocked> <blocker> # Remove dependency

# Priorities (workspace-defined, resolved by name)
zh priorities                 # List the workspace's configured priorities
zh priority <issue> "High priority"  # Set by name (case-insensitive); errors if not configured
zh priority <issue> clear     # Remove priority

# Planning hierarchy (issue-type + sub-issue model)
# ZenHub removed Legacy Epics and ZenhubEpics in June 2025. An epic is now a
# normal issue whose issue-type is Epic; children are wired via sub-issues.
# Levels: 1 Initiative, 2 Project, 3 Epic (planning panel); 4 Bug/Feature/Task,
# 5 Sub-task (board). Each ZenHub-managed level has a noun with the same surface.
zh epic list                       # List Epic issues
zh epic show <issue#>              # Show the issue + its sub-issues
zh epic create "Title" [-d body] [-l labels] [-p pipeline] [--json|-q]
zh epic update <issue#> [-t "New Title"] [-d "New body"]
zh epic add <parent#> <issue#> [...]    # Attach sub-issues
zh epic remove <parent#> <issue#> [...] # Detach sub-issues
zh epic close <issue#> [comment]   # Close the issue
zh epic reopen <issue#>            # Reopen the issue
zh initiative <sub> / zh project <sub> / zh subtask <sub>  # Same surface, other levels
# To delete an epic, delete the issue: zh delete <issue#> (DANGER, prefer close)

# Sub-issues (the parent/child wiring; epic add is sugar over this)
zh subissue add <parent#> <child#> [...]      # Link issues as sub-issues
zh subissue remove <parent#> <child#> [...]   # Unlink sub-issues
zh subissue list <parent#>                    # List sub-issues of a parent
zh subissue reorder <child#> top|bottom|after <sib#>|before <sib#>
                                              # Reorder among siblings (sibling-anchored, not integer positions)

# Sprints — read
zh sprints [--all]                            # List sprints (● marks active; --all includes closed)
zh sprint <name>                              # Sprint detail + issues
zh sprint current                             # Active sprint
zh sprint                                     # Defaults to current
zh sprint <name> --no-urls                    # Compact output

# Sprints — write
zh sprint add <name> <issue#> [<issue#> ...]    # Add issues to a sprint
zh sprint remove <name> <issue#> [<issue#> ...] # Remove issues from a sprint
zh sa current 42                              # Top-level alias for sprint add
zh sr current 42                              # Top-level alias for sprint remove

# Global flags (peeled off before any subcommand)
zh -r owner/repo <cmd>                        # Target a specific repo
zh -w "Backend Team" <cmd>                    # Target a specific workspace
# Or set ZH_REPO / ZH_WORKSPACE in env or ~/.config/zh/config

# Discovery
zh types                # List assignable issue types (name, level, disposition, source)
zh priorities           # List configured priorities
zh labels               # List labels
zh pipelines            # List pipelines
zh workspaces           # List workspaces (● marks active target)
```

### Important Patterns

1. **Issue numbers**: Can use `#` prefix or not: `zh issue 42` or `zh issue #42`
2. **Pipeline names**: Case-insensitive, partial matching: `zh pipeline todo` matches "TO DO"
3. **Closed issues**: Use `--all` flag to include closed: `zh board --all`
4. **ZenHub URLs**: Shown by default in `mine` and `pipeline`; use `--no-urls` to hide
5. **Multi-repo workspaces**: Output shows repo name for each issue (issues may come from different repos)
6. **Repo / workspace overrides**: `-r owner/repo` and `-w "Name"` work in front of any subcommand. Persist via `ZH_REPO` / `ZH_WORKSPACE` in `~/.config/zh/config`.
7. **GitHub-backed scope**: Issue numbers resolve through GitHub, so issue-level commands target the GitHub issue behind a card. **ZenHub-only cards** (no GitHub issue; shown as `NoOwner/<repo>` with `…/issues/zh/<n>` URLs — e.g. a workspace's seed card) are not reliably addressable — `1` resolves to GitHub #1, not `zh/1`. Direct the user to the ZenHub web UI for those.

## For AI Assistants

### When to Use This Tool

Use `zh` when the user asks about:
- Sprint planning or backlog management
- Moving, prioritizing, or estimating issues
- Creating new tickets/issues
- Checking what's assigned to someone
- Board or pipeline status

### Best Practices

1. **Start with discovery**: Run `zh board` or `zh pipelines` first to understand the workspace
2. **Check before creating**: Run `zh types` and `zh labels` before creating issues
3. **Confirm destructive actions**: Moving issues or changing estimates affects the team
4. **Use full pipeline names**: When moving issues, use exact pipeline names from `zh pipelines`

### Example Workflows

**Creating a bug ticket:**
```bash
# First, discover available options
zh types
zh labels

# Then create with appropriate metadata
zh create "Login fails with special characters" \
  -t Bug \
  -l "bug,frontend" \
  -e 2 \
  -p "TO DO" \
  -b "Users report login failing when password contains & or #"
```

**Sprint planning session:**
```bash
# Get overview
zh board

# Review backlog
zh pipeline "Product Backlog"

# Move items to sprint
zh move 123 "TO DO"
zh move 456 "TO DO"

# Prioritize
zh reorder 123 top
zh reorder 456 1

# Estimate
zh estimate 123 3
zh estimate 456 5
```

**Checking blockers:**
```bash
# View issue to see blocking relationships
zh issue 42
# Output includes "Blocked by: #41" if blocked
```

**Closing completed work:**
```bash
# Close an issue when done
zh close 123

# Close with a comment
zh close 123 "Completed in PR #456"

# Reopen if needed
zh reopen 123
```

## Project Structure

```
zenhub-cli/
├── zh                  # Main executable (bash script)
├── mcp_server.py       # MCP server entry point (FastMCP + tool defs)
├── zh_api.py           # GraphQL client + auth/repo/workspace resolution
├── zh_graphql_ops.py   # ZenHub GraphQL ops (sub-issues + sprints)
├── similarity.py       # Sentence-embedding duplicate detection
├── tests/              # pytest suite (mocks the network)
├── agents/zenhub.md    # Generic agent definition (copy to ~/.claude/agents/)
├── README.md           # User documentation
├── CLAUDE.md           # This file (AI assistant guidance)
├── CONTRIBUTING.md     # Contribution workflow
├── SECURITY.md         # Security posture
├── LICENSE             # MIT license
├── VERSION             # Current version number
└── .github/workflows/  # CI + Claude review workflows
```

## Configuration

The tool reads tokens from `~/.config/zh/config`:

```bash
ZH_TOKEN=...        # GraphQL API token (most commands)
ZH_REST_TOKEN=...   # REST API token (unblock command only)
```

### Additional environment variables

`zh` and the MCP server honor these env vars; mirror in sync with the README's env-vars table and `mcp_server.py`'s module docstring.

**CLI**

| Variable | Purpose |
|---|---|
| `ZH_REPO` | Default `owner/repo` for `zh` invocations; overridden by `-r owner/repo`. |
| `ZH_WORKSPACE` | Default workspace name; overridden by `-w "Workspace Name"`. Precedence: flag > env / config > git-remote + first-workspace fallback. |

**MCP server**

| Variable | Purpose |
|---|---|
| `ZH_DEFAULT_REPO_PATH` | Default git checkout directory the MCP runs `zh` from when a tool call omits `repo_path`. |
| `ZH_BIN_PATH` | Path to the `zh` bash script (default: peer to `mcp_server.py`). Useful for testing alternate `zh` builds. |
| `ZH_MCP_VENV` | Full absolute path of the venv the MCP server bootstraps and re-execs into. Overrides the XDG default. Empty / whitespace values are warned and ignored; relative paths are rejected. |
| `XDG_DATA_HOME` | Standard XDG override for the data root. The venv lives at `$XDG_DATA_HOME/zh/venv` (default `~/.local/share/zh/venv`). |
| `ZH_MCP_PROBE_TIMEOUT` | Seconds for the per-launch `import` probe that validates the venv (default 30). Widen on slow media (NFS home, FileVault cold cache) to avoid a probe timeout triggering a needless rebuild. |
| `ZH_MCP_SKIP_BOOTSTRAP` | Test-mode escape hatch. Setting `=1` skips the venv build + execv and substitutes a no-op `FastMCP` stub so the pytest suite can exercise the MCP tool functions without pulling in `mcp` / `torch` / `transformers`. **Never set in production.** |

## Development Notes

- Single bash script, no build process
- Uses `jq` for JSON processing
- Uses `gh` CLI for GitHub API calls
- GraphQL API at `https://api.zenhub.com/public/graphql`
- REST API at `https://api.zenhub.com/p1/` (deprecated, only used for unblock)

## Error Handling

Common errors and solutions:
- "Not in a git repository": Must run from a git repo with GitHub remote
- "Could not get GitHub repo ID": Ensure `gh auth status` shows authenticated
- "ZenHub API error": Check token validity and repository workspace connection
