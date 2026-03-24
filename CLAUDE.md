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

# Sprints
zh sprints                        # List open sprints (● = active)
zh sprints --all                  # Include closed sprints
zh sprint "Sprint 5"              # View sprint details and issues
zh sprint current                 # View active sprint
zh sprint-add <issue> "Sprint"    # Add issue to sprint
zh sprint-add <issue> current     # Add issue to active sprint
zh sprint-remove <issue> "Sprint" # Remove issue from sprint

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

# Issue type & hierarchy
zh set-type <issue> Epic      # Set issue type
zh set-type <issue> clear     # Remove issue type
zh set-parent <issue> <parent>    # Make issue a sub-issue
zh remove-parent <issue> <parent> # Remove parent relationship
zh children <issue>           # List sub-issues

# Dependencies & priority
zh block <blocked> <blocker>  # Set dependency
zh unblock <blocked> <blocker> # Remove dependency
zh priority <issue> high      # Set priority

# Discovery
zh types                # List issue types
zh labels               # List labels
zh pipelines            # List pipelines
zh workspaces           # List available workspaces

# Workspace targeting (global -w flag)
zh -w "Backend" board               # Board for a specific workspace
zh -w "Backend" sprints             # Sprints in a specific workspace
zh -w "Backend" sprint current      # Active sprint in a workspace
zh -w "Backend" move 42 "Done"      # Move issue in a specific workspace
zh -w "Backend" pipeline "TO DO"    # Pipeline in a specific workspace
```

### Important Patterns

1. **Issue numbers**: Can use `#` prefix or not: `zh issue 42` or `zh issue #42`
2. **Pipeline names**: Case-insensitive, partial matching: `zh pipeline todo` matches "TO DO"
3. **Closed issues**: Use `--all` flag to include closed: `zh board --all`
4. **ZenHub URLs**: Shown by default in `mine` and `pipeline`; use `--no-urls` to hide
5. **Multi-repo workspaces**: Output shows repo name for each issue (issues may come from different repos)
6. **Sprint names**: Case-insensitive; use `current` or `active` as alias for the active sprint
7. **Workspace targeting**: Use `-w "Name"` before any command to target a specific workspace, or set `ZH_WORKSPACE` in config

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
zh sprints

# Review backlog and current sprint
zh pipeline "Product Backlog"
zh sprint current

# Add items to sprint and move to pipeline
zh sprint-add 123 current
zh sprint-add 456 current
zh move 123 "TO DO"
zh move 456 "TO DO"

# Prioritize and estimate
zh reorder 123 top
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
ZH_WORKSPACE=...    # Default workspace name (optional, uses first found if unset)
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
