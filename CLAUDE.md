# CLAUDE.md

This file provides guidance to Claude Code and other AI assistants when working with the ZenHub CLI tool.

## Project Overview

`zh` is a bash-based command-line interface for ZenHub that wraps both the GraphQL and REST APIs. It's designed to be used directly by developers or by AI assistants helping with project management tasks.

## Quick Reference

### Common Commands

```bash
# View issues and board
zh issue <number>       # View issue details
zh mine                 # Your assigned issues
zh mine @username       # Issues assigned to user
zh users                # List assignable users
zh board                # Board overview
zh pipeline "Name"      # Issues in a pipeline

# Manage issues
zh move <issue> "Pipeline"    # Move to pipeline
zh reorder <issue> top        # Prioritize
zh estimate <issue> <points>  # Set estimate
zh assign <issue> <user>      # Assign user
zh unassign <issue> [user]    # Remove assignee(s)
zh comment <issue> "text"     # Add comment

# Create issues
zh create "Title" -t Bug -l "label1,label2" -e 3 -p "TO DO"

# Dependencies & priority
zh block <blocked> <blocker>  # Set dependency
zh unblock <blocked> <blocker> # Remove dependency
zh priority <issue> high      # Set priority

# Discovery
zh types                # List issue types
zh labels               # List labels
zh pipelines            # List pipelines
```

### Important Patterns

1. **Issue numbers**: Can use `#` prefix or not: `zh issue 42` or `zh issue #42`
2. **Pipeline names**: Case-insensitive, partial matching: `zh pipeline todo` matches "TO DO"
3. **Closed issues**: Use `--all` flag to include closed: `zh board --all`

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
