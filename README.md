# ZenHub CLI (`zh`)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub release](https://img.shields.io/github/v/release/daniel-pittman/zenhub-cli)](https://github.com/daniel-pittman/zenhub-cli/releases)

A powerful command-line interface for ZenHub. Manage issues, pipelines, sprints, and more directly from your terminal.

## Features

- 📋 **View & manage issues** - See details, move between pipelines, set estimates
- 🔄 **Sprint planning** - Board overview, pipeline management, bulk operations
- 🎯 **Prioritization** - Reorder issues, set priorities, manage dependencies
- ✨ **Create issues** - Full support for types, labels, assignees, and descriptions
- 🔗 **ZenHub URLs** - Clickable links to issues in the ZenHub board
- 📁 **Multi-repo workspaces** - Works with workspaces containing multiple repositories
- 🤖 **AI-friendly** - Designed for use with AI assistants like Claude

## Requirements

Before installing, ensure you have these dependencies:

| Dependency | Purpose | Installation |
|------------|---------|--------------|
| **bash** | Shell (v4.0+) | Pre-installed on macOS/Linux |
| **curl** | HTTP requests | Pre-installed on most systems |
| **jq** | JSON processing | `brew install jq` or `apt install jq` |
| **git** | Repository detection | `brew install git` or `apt install git` |
| **gh** | GitHub CLI | `brew install gh` or see [cli.github.com](https://cli.github.com) |

### Verify Dependencies

```bash
# Check all dependencies are installed
command -v bash curl jq git gh >/dev/null && echo "All dependencies installed!" || echo "Missing dependencies"

# Ensure GitHub CLI is authenticated
gh auth status
```

## Installation

### Option 1: Clone and Symlink (Recommended)

```bash
# Clone the repository
git clone https://github.com/daniel-pittman/zenhub-cli.git ~/.zenhub-cli

# Add to your PATH (choose one):

# For bash (~/.bashrc or ~/.bash_profile):
echo 'export PATH="$HOME/.zenhub-cli:$PATH"' >> ~/.bashrc
source ~/.bashrc

# For zsh (~/.zshrc):
echo 'export PATH="$HOME/.zenhub-cli:$PATH"' >> ~/.zshrc
source ~/.zshrc

# Or create a symlink:
sudo ln -sf ~/.zenhub-cli/zh /usr/local/bin/zh
```

### Option 2: Direct Download

```bash
# Download the script
curl -o ~/.local/bin/zh https://raw.githubusercontent.com/daniel-pittman/zenhub-cli/main/zh
chmod +x ~/.local/bin/zh

# Ensure ~/.local/bin is in your PATH
```

### Verify Installation

```bash
zh help
```

## Configuration

### Step 1: Generate API Tokens

You need **two tokens** from ZenHub:

| Token | URL | Used For |
|-------|-----|----------|
| **GraphQL API** | [app.zenhub.com/settings/tokens](https://app.zenhub.com/settings/tokens) | Most commands |
| **REST API** | [app.zenhub.com/dashboard/tokens](https://app.zenhub.com/dashboard/tokens) | `unblock` command |

> **Note:** The GraphQL API doesn't support removing dependencies yet. The REST API token is only needed if you use the `unblock` command. Consider [requesting this feature](https://community.zenhub.com/) from ZenHub.

### Step 2: Create Config File

```bash
mkdir -p ~/.config/zh
cat > ~/.config/zh/config << 'EOF'
ZH_TOKEN=your_graphql_token_here
ZH_REST_TOKEN=your_rest_token_here
EOF

# Secure the file (tokens are sensitive!)
chmod 600 ~/.config/zh/config
```

### Alternative: Project-level Config

You can also create a `.env` file in your project directory:

```bash
# .env (add to .gitignore!)
ZH_TOKEN=your_graphql_token_here
ZH_REST_TOKEN=your_rest_token_here
```

## Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `issue <number>` | `i`, `show` | View issue details |
| `mine [user]` | `my` | List issues assigned to you (or specified user) |
| `board [--all]` | `b`, `overview` | Show board overview with issue counts |
| `pipeline <name> [--all]` | `pipe`, `col` | List issues in a specific pipeline |
| `pipelines [repo]` | `pipes`, `p` | List pipeline names for a workspace |
| `move <issue> <pipeline>` | `mv`, `m` | Move an issue to a pipeline |
| `reorder <issue> <position>` | `order`, `pos` | Reorder issue within its pipeline |
| `estimate <issue> <points>` | `est`, `points` | Set story point estimate |
| `assign <issue> <user>` | | Assign a user to an issue |
| `unassign <issue> [user]` | | Remove assignee(s) from an issue |
| `comment <issue> [text]` | `c` | Add a comment to an issue |
| `attach <issue>` | | Open issue in browser to add attachments |
| `close <issue> [comment]` | | Close an issue |
| `reopen <issue>` | | Reopen a closed issue |
| `create <title> [options]` | `new` | Create a new issue |
| `block <issue> <blocker>` | `blocked-by`, `depends` | Set issue as blocked by another |
| `unblock <issue> <blocker>` | | Remove a blocking dependency |
| `priority <issue> [level]` | `prio` | Set or view issue priority |
| `sprints [--all]` | `sp` | List sprints in workspace |
| `sprint <name>` | | View sprint details and issues |
| `sprint-add <issue> <sprint>` | `sa` | Add an issue to a sprint |
| `sprint-remove <issue> <sprint>` | `sr` | Remove an issue from a sprint |
| `types` | | List available issue types |
| `labels` | | List available labels |
| `users` | | List users who can be assigned to issues |
| `workspaces` | `ws` | List available workspaces |
| `help` | `-h`, `--help` | Show help message |

## Usage Examples

### View Issues

```bash
# View details of an issue (includes blocking relationships)
zh issue 42

# See issues assigned to you
zh mine

# See issues assigned to a specific user
zh mine daniel-pittman

# Board overview (open issues only by default)
zh board

# Board overview including closed issues
zh board --all
```

### Browse Pipelines

```bash
# List all pipeline names
zh pipelines

# List issues in a pipeline (open only by default)
zh pipeline "TO DO"
zh pipeline "In Progress"

# Include closed issues
zh pipeline "Done" --all
```

### Move & Prioritize Issues

```bash
# Move issue to a different pipeline
zh move 42 "In Progress"
zh move #42 done          # # prefix and case-insensitive matching

# Reorder within current pipeline
zh reorder 42 top         # Move to top
zh reorder 42 0           # Same as top
zh reorder 42 bottom      # Move to bottom
zh reorder 42 5           # Move to position 5
```

### Estimates & Assignment

```bash
# Set story points
zh estimate 42 3
zh estimate 42 0.5        # Decimals supported
zh estimate 42 clear      # Remove estimate

# Assign users
zh assign 42 username
zh assign 42 @username    # @ prefix works too

# Remove assignees
zh unassign 42 username   # Remove specific user
zh unassign 42            # Remove all assignees
```

### Comments

```bash
# Add inline comment
zh comment 42 "Fixed in PR #99"

# With -m flag
zh comment 42 -m "Still investigating this issue"

# From file (for longer comments)
zh comment 42 -f ./investigation-notes.md

# From stdin (useful for piping)
echo "Automated update: build passed" | zh comment 42 --stdin
```

### Create Issues

```bash
# Simple issue
zh create "Fix login bug"

# With type and labels
zh create "Fix login bug" -t Bug -l "frontend,bug"

# Full options
zh create "Add dark mode" \
  -t Feature \
  -a username \
  -e 5 \
  -p "TO DO" \
  -l "frontend,enhancement"

# With description from file
zh create "Complex feature" -t Feature -f ./description.md

# With description from stdin
cat << 'EOF' | zh create "New feature" -t Feature --stdin
## Description
This feature adds...

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2
EOF
```

**Create options:**
- `-t, --type <type>` - Issue type (Bug, Feature, Task, Spike, etc.)
- `-l, --labels <labels>` - Comma-separated labels
- `-a, --assignee <user>` - GitHub username to assign
- `-p, --pipeline <name>` - Pipeline to place issue in
- `-e, --estimate <pts>` - Story points
- `-b, --body <text>` - Short description inline
- `-f, --file <path>` - Read description from file
- `--stdin` - Read description from stdin

### Close & Reopen Issues

```bash
# Close an issue (moves to Closed pipeline in ZenHub)
zh close 42

# Close with a comment
zh close 42 "Completed in PR #99"

# Reopen a closed issue
zh reopen 42
```

### Dependencies & Priority

```bash
# Set issue #123 as blocked by #456
# (Can't work on 123 until 456 is done)
zh block 123 456

# Remove blocking dependency
zh unblock 123 456

# View current priority
zh priority 42

# Set high priority
zh priority 42 high

# Remove priority
zh priority 42 clear
```

### Sprints

```bash
# List open sprints (● marks the active sprint)
zh sprints

# Include closed sprints
zh sprints --all

# View active sprint details and issues
zh sprint current

# View a specific sprint
zh sprint "Sprint 5"
zh sprint "Sprint 5" --no-urls   # Compact output

# Add an issue to a sprint
zh sprint-add 42 "Sprint 5"
zh sprint-add 42 current         # Add to active sprint

# Remove an issue from a sprint
zh sprint-remove 42 "Sprint 5"
zh sprint-remove 42 current

# Move an issue between sprints
zh sprint-remove 42 "Sprint 4"
zh sprint-add 42 "Sprint 5"
```

### Discovery Commands

```bash
# List available issue types
zh types

# List available labels
zh labels

# List users who can be assigned to issues
zh users

# List workspaces
zh workspaces
```

## Output Options

### ZenHub URLs

By default, `zh mine` and `zh pipeline` show clickable ZenHub URLs for each issue:

```bash
$ zh mine

Issues assigned to daniel-pittman (3):

  #98 │ msu-denver/sustainability-hub │ Product Backlog
    Fix login bug
    → https://app.zenhub.com/workspaces/.../issues/gh/msu-denver/sustainability-hub/98
```

Use `--no-urls` for compact output:

```bash
zh mine --no-urls
zh pipeline "TO DO" --no-urls
```

### Filtering

By default, `board` and `pipeline` commands exclude closed issues to reduce noise:

```bash
zh board              # Open issues only (default)
zh board --all        # All issues including closed

zh pipeline todo      # Open issues only (default)
zh pipeline todo -a   # All issues including closed
```

## Workflows

### Sprint Planning

```bash
# 1. Get the big picture
zh board
zh sprints

# 2. Review backlog and current sprint
zh pipeline "Product Backlog"
zh sprint current

# 3. Add items to the sprint
zh sprint-add 123 current
zh sprint-add 456 current

# 4. Move to pipeline
zh move 123 "TO DO"
zh move 456 "TO DO"

# 5. Prioritize the sprint
zh reorder 123 top
zh reorder 456 1

# 6. Set estimates
zh estimate 123 3
zh estimate 456 5

# 7. Assign work
zh assign 123 developer1
zh assign 456 developer2

# 8. Check specific issues
zh issue 123
```

### AI-Assisted Workflows

The CLI is designed for use with AI assistants like Claude, GitHub Copilot, or ChatGPT:

**Ticket Creation:**
```
User: Create a bug ticket for the login issue we discussed

AI: [runs zh labels, zh types to see options]
    [runs zh create "Login fails with special characters" \
          -t Bug -l "bug,frontend" -e 2 -p "TO DO" \
          -b "Users report login failing when password contains & or #"]
```

**Sprint Review:**
```
User: What's in our current sprint?

AI: [runs zh sprint current]

    Summarizes work, progress, and blockers
```

**Bulk Operations:**
```
User: Point all the unpointed bugs at 2

AI: [runs zh pipeline "TO DO"]
    [identifies unpointed bugs]
    [runs zh estimate for each]
```

## Troubleshooting

### "Not in a git repository"
The CLI auto-detects your repository from the git remote. Run commands from within a git repository that has a GitHub remote configured.

### "Could not get GitHub repo ID"
Ensure the GitHub CLI is authenticated: `gh auth status`

### "ZenHub API error"
- Check your token is valid and not expired
- Ensure you're using the correct token type (GraphQL vs REST)
- Verify the repository is connected to a ZenHub workspace

### "Issue not found"
The issue must exist in a repository that's part of your ZenHub workspace.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built for use with [ZenHub](https://www.zenhub.com/)
- Inspired by the need for efficient terminal-based project management
- Designed with AI-assisted workflows in mind
