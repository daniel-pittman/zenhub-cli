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

# Create issues
zh create "Title" -t Bug -l "label1,label2" -e 3 -p "TO DO"

# Dependencies & priority
zh block <blocked> <blocker>  # Set dependency
zh unblock <blocked> <blocker> # Remove dependency
zh priority <issue> high      # Set priority

# Epics (ZenHub native epics, workspace-scoped)
zh epic list                       # List all epics in workspace
zh epic show <epic#>               # Show epic + child issues
zh epic create "Title" [-d body] [-l labels]
zh epic update <epic#> [-t "New Title"] [-d "New body"]  # Edit existing epic
zh epic add <epic#> <issue#> [...]    # Add one or more issues to an epic
zh epic remove <epic#> <issue#> [...] # Remove one or more issues from an epic
zh epic close <epic#>              # Close epic
zh epic reopen <epic#>             # Reopen epic
zh epic delete <epic#>             # DANGER: permanently delete an epic

# Sub-issues (3rd hierarchy tier: Epic -> Issue -> Sub-issue)
zh subissue add <parent#> <child#> [...]      # Link issues as sub-issues
zh subissue remove <parent#> <child#> [...]   # Unlink sub-issues
zh subissue list <parent#>                    # List sub-issues of a parent
zh subissue reorder <child#> top|bottom|after <sib#>|before <sib#>
                                              # Reorder among siblings (sibling-anchored, not integer positions)

# Discovery
zh types                # List issue types
zh labels               # List labels
zh pipelines            # List pipelines
```

### Important Patterns

1. **Issue numbers**: Can use `#` prefix or not: `zh issue 42` or `zh issue #42`
2. **Pipeline names**: Case-insensitive, partial matching: `zh pipeline todo` matches "TO DO"
3. **Closed issues**: Use `--all` flag to include closed: `zh board --all`
4. **ZenHub URLs**: Shown by default in `mine` and `pipeline`; use `--no-urls` to hide
5. **Multi-repo workspaces**: Output shows repo name for each issue (issues may come from different repos)

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
├── zh              # Main executable (bash script)
├── README.md       # User documentation
├── CLAUDE.md       # This file (AI assistant guidance)
├── LICENSE         # MIT license
├── VERSION         # Current version number
└── .gitignore      # Git ignore patterns
```

## Configuration

The tool reads tokens from `~/.config/zh/config`:

```bash
ZH_TOKEN=...        # GraphQL API token (most commands)
ZH_REST_TOKEN=...   # REST API token (unblock command only)
```

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
