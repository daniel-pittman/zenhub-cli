# ZenHub CLI (`zh`)

![ZenHub CLI — manage ZenHub issues, pipelines, sprints, and sub-issues from your terminal](docs/img/social-preview.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub release](https://img.shields.io/github/v/release/daniel-pittman/zenhub-cli)](https://github.com/daniel-pittman/zenhub-cli/releases)
[![CI](https://github.com/daniel-pittman/zenhub-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/daniel-pittman/zenhub-cli/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A powerful command-line interface for ZenHub. Manage issues, pipelines, sprints, and more directly from your terminal.

> **Scope: GitHub-backed issues.** `zh` is designed for workspaces whose issues are backed by GitHub. It resolves issue numbers through GitHub (via `gh` and ZenHub's GitHub-issue lookups), so every issue-level command (`close`, `reopen`, `delete`, `move`, `assign`, etc.) targets the GitHub issue behind a card. ZenHub also supports **ZenHub-only cards** — cards with no GitHub issue behind them (they show as `NoOwner/<repo>` and have `…/issues/zh/<n>` URLs, e.g. the seed card ZenHub drops into a new workspace). `zh` cannot reliably address those: a number like `1` resolves to GitHub issue/PR #1, not the ZenHub-only card `zh/1`. **Manage ZenHub-only cards in the ZenHub web UI**, where commands here may not behave as expected.

## Features

- 📋 **View & manage issues** - See details, move between pipelines, set estimates
- 🔄 **Sprint planning** - Board overview, pipeline management, bulk operations
- 🎯 **Prioritization** - Reorder issues, set priorities, manage dependencies
- ✨ **Create issues** - Full support for types, labels, assignees, and descriptions
- 🔗 **ZenHub URLs** - Clickable links to issues in the ZenHub board
- 📁 **Multi-repo workspaces** - Works with workspaces containing multiple repositories
- 🤖 **AI-friendly** - Designed for use with AI assistants like Claude
- 🔍 **Duplicate detection** *(MCP only)* - Sentence-embedding similarity search catches paraphrased duplicates before creating issues
- 🤖 **Bundled Claude Code agent** - Drop-in `agents/zenhub.md` adds intelligent behavior layer on top of the tools (propose-first for destructive ops, batch audit trails, proactive duplicate detection)

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
# Check all dependencies are installed (reports each missing tool by name)
missing=""; for dep in bash curl jq git gh; do command -v "$dep" >/dev/null 2>&1 || missing="$missing $dep"; done; [ -z "$missing" ] && echo "All dependencies installed!" || echo "Missing dependencies:$missing"

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
# Optional: default repo + workspace (override with -r / -w on any call)
# ZH_REPO=owner/repo
# ZH_WORKSPACE="My Workspace"
EOF

# Secure the file (tokens are sensitive!)
chmod 600 ~/.config/zh/config
```

### Repo + workspace targeting

`zh` infers the GitHub owner/repo from `git remote get-url origin` and picks the first ZenHub workspace the repo is connected to. Override either with global flags or config:

```bash
# One-off override on a single invocation
zh -r owner/repo board
zh -w "Backend Team" sprints
zh -r owner/repo -w "Backend Team" sprint current

# Per-shell or persistent defaults
export ZH_REPO="acme/widgets"
export ZH_WORKSPACE="Backend Team"
# or write them into ~/.config/zh/config
```

Precedence (highest first): `-r` / `-w` flag → `ZH_REPO` / `ZH_WORKSPACE` env or config → git-remote + first-workspace fallback.

`zh workspaces` shows every workspace the repo is connected to and marks which one the rest of the CLI would currently target.

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
| `assign <issue> <user> [user...]` | | Assign one or more users to an issue |
| `unassign <issue> <user> [user...]` | | Remove the named assignee(s) |
| `unassign <issue> --all` | | Remove ALL assignees (explicit) |
| `comment <issue> [text]` | `c` | Add a comment to an issue |
| `attach <issue>` | | Open issue in browser to add attachments |
| `close <issue> [comment]` | | Close an issue |
| `reopen <issue>` | | Reopen a closed issue |
| `delete <issue> [-y]` | | **Permanently delete** a GitHub issue (via `gh`; needs admin/triage). Prompts to confirm when interactive; `-y`/`--yes` skips. Prefer `close`. |
| `create <title> [options]` | `new` | Create a new issue (`--json` / `-q` for machine output, `--parent` to nest) |
| `block <issue> <blocker>` | `blocked-by`, `depends` | Set issue as blocked by another |
| `unblock <issue> <blocker>` | | Remove a blocking dependency |
| `priority <issue> [name]` | `prio` | Set or view issue priority by name (workspace-defined; see [Priorities](#priorities)) |
| `priorities` | `prios` | List the workspace's configured priorities |
| `type <issue> <name>` | `set-type`, `retype` | Change an existing issue's type |
| `epic <subcommand>` | `epics` | Manage Epic issues (issue-type + sub-issues; see [Planning hierarchy](#planning-hierarchy)) |
| `initiative <subcommand>` | `initiatives` | Manage Initiative issues (level 1) |
| `project <subcommand>` | `projects` | Manage Project issues (level 2) |
| `subtask <subcommand>` | `subtasks` | Manage Sub-task issues (level 5) |
| `subissue <subcommand>` | `subissues`, `sub`, `child`, `children` | Manage sub-issues, the parent/child wiring (see [Sub-issues](#sub-issues)) |
| `sprints [--all]` | `sp` | List sprints in workspace (see [Sprints](#sprints)) |
| `sprint <name>` | | View sprint details and issues |
| `sprint add <name> <issue#> [...]` | `sa` (top-level) | Add one or more issues to a sprint |
| `sprint remove <name> <issue#> [...]` | `sr` (top-level), `rm` | Remove one or more issues from a sprint |
| `types` | | List assignable issue types (name, level, disposition, source) |
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
zh mine acme-user

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
zh assign 42 @username       # @ prefix works too
zh assign 42 alice bob       # assign multiple users at once

# Remove assignees
zh unassign 42 username      # Remove only the named user
zh unassign 42 alice bob     # Remove multiple named users
zh unassign 42 --all         # Remove ALL assignees (explicit)
# Note: `zh unassign 42` with no user now errors instead of clearing everyone,
# so a missing/mistyped target can't silently un-assign teammates.
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

# Machine-readable output for batch / agent callers (clean JSON on stdout,
# human chatter goes to stderr)
zh create "Auth service" -t Epic --json
# {"number":42,"url":"https://github.com/o/r/issues/42","title":"Auth service",
#  "type":"Epic","pipeline":null,"estimate":null,"estimate_requested":null,
#  "parent":null,"priority":null,"priority_requested":null}
#
# v1.9.1: `priority_requested` mirrors the user's --priority value
# regardless of whether the post-create mutation confirmed it. Compare to
# detect partial apply: `priority_requested="High", priority=null` means
# the request did not land and `zh priority #N "High"` is the safe retry.
#
# v1.9.2: the same three-state split now applies to `estimate`. Compare
# `estimate_requested` vs `estimate`: null/null means not requested,
# N/N means applied, N/null means requested but the setEstimate
# mutation did not confirm (retry with `zh estimate #N <pts>`).

# Or just the new number
NEW=$(zh create "Quick task" -q)
```

**Create options:**
- `-t, --type <type>` - Issue type (discover with `zh types`; any assignable type works, including Epic/Initiative/Project/Sub-task)
- `-l, --labels <labels>` - Comma-separated labels
- `-a, --assignee <user>` - GitHub username to assign
- `-p, --pipeline <name>` - Pipeline to place issue in
- `-e, --estimate <pts>` - Story points
- `--parent <issue#>` - Wire the new issue as a sub-issue of `<issue#>`
- `--priority <name>` - Set a configured priority by name at create time (resolved case-insensitively against `zh priorities`; same name-resolution path as `zh priority`)
- `-b, --body <text>` - Short description inline
- `-f, --file <path>` - Read description from file
- `--stdin` - Read description from stdin
- `--json` - Emit a JSON object on stdout (suppresses human lines)
- `-q, --quiet` - Emit only the new issue number on stdout

### Close & Reopen Issues

```bash
# Close an issue (moves to Closed pipeline in ZenHub)
zh close 42

# Close with a comment
zh close 42 "Completed in PR #99"

# Reopen a closed issue
zh reopen 42

# PERMANENTLY delete a GitHub issue (DANGER — prefer `close`; needs
# admin/triage permission). Deletes via `gh issue delete`, so the card
# also disappears from the ZenHub board. ZenHub-only cards (no GitHub
# issue behind them) must be removed in the ZenHub web UI.
# When run interactively you'll be asked to retype the issue number to
# confirm; -y/--yes skips the prompt (also implied for non-interactive
# callers such as agents, pipes, and CI).
zh delete 42
zh delete 42 -y   # skip the confirmation prompt
```

### Dependencies

```bash
# Set issue #123 as blocked by #456
# (Can't work on 123 until 456 is done)
zh block 123 456

# Remove blocking dependency
zh unblock 123 456
```

### Priorities

Priorities are **workspace-defined**, not a fixed high/medium/low set. `zh priority` resolves the name you pass case-insensitively against the workspace's configured priorities. Discover the configured names with `zh priorities`.

```bash
# List the workspace's configured priorities
zh priorities

# View an issue's current priority
zh priority 42

# Set a priority by name (matched case-insensitively)
zh priority 42 "High priority"

# Remove priority
zh priority 42 clear
```

If you pass a name that isn't configured, `zh` errors and lists the available names rather than silently doing the wrong thing. Add new priorities in your ZenHub workspace settings.

### Planning hierarchy

ZenHub removed Legacy Epics and ZenhubEpics in June 2025: "Epics and Projects have been replaced with Issue Types and Sub-Issues." The current model is a 5-level issue-type hierarchy wired with Sub-Issues:

| Level | Type | Disposition |
|-------|------|-------------|
| 1 | Initiative | Planning panel |
| 2 | Project | Planning panel |
| 3 | Epic | Planning panel |
| 4 | Bug / Feature / Task | Board |
| 5 | Sub-task | Board |

An **epic is just an issue** whose issue-type is Epic. It has an ordinary GitHub issue number and issue URL; children are attached via Sub-Issues. `zh` exposes a planning noun for each ZenHub-managed level (`initiative`, `project`, `epic`, `subtask`), each a thin wrapper over the same issue-type + sub-issue machinery. Board-level Bug/Feature/Task are created with `zh create -t <type>`.

```bash
# List the workspace's assignable types with level + disposition + source
zh types

# List Epic issues
zh epic list

# Create an Epic (any of -d body, -l labels, -p pipeline, --json / -q)
zh epic create "Auth Service" -d "Build authentication API endpoints" -l backend,auth

# Show an epic issue and its child issues
zh epic show 42

# Edit an epic issue's title and/or body
zh epic update 42 -t "Auth Service (v2)" -d "Updated body text"

# Attach / detach sub-issues (one OR MORE per call)
zh epic add 42 369 370 412
zh epic remove 42 369

# Close / reopen the epic issue
zh epic close 42 "Shipped in release X"
zh epic reopen 42

# The other planning levels share the same surface
zh initiative create "Platform modernization"
zh project list
zh subtask create "Wire up the migration script" --parent 42
```

You can also change an existing issue's type at any time, and create a child in one call:

```bash
# Promote an existing issue to an Epic (or any configured type)
zh type 42 Epic

# Create a Feature already nested under epic #42
zh create "Token refresh endpoint" -t Feature --parent 42 --json
```

**Planning-noun subcommands (same for initiative / project / epic / subtask):**

| Subcommand | Description |
|------------|-------------|
| `list` | List issues of that type (number, state, title) |
| `show <issue#>` | Show the issue + its child issues |
| `create "Title" [opts]` | Create an issue of that type. Options: `-d` body, `-l` labels, `-p` pipeline, `--json` / `-q` |
| `update <issue#> [opts]` | Edit title and/or body. Options: `-t`, `-d`. Aliases: `edit`, `modify` |
| `add <parent#> <issue#>...` | Attach sub-issues (single API call) |
| `remove <parent#> <issue#>...` | Detach sub-issues |
| `close <issue#> [comment]` | Close the issue |
| `reopen <issue#>` | Reopen the issue |

To delete an epic, delete the issue: `zh delete <issue#>` (DANGER, prefer `close`).

### Sub-issues

ZenHub supports a 3rd hierarchy tier below Epic → Issue: **sub-issues**. A sub-issue is a regular Issue whose `parentIssue` field points to another Issue. Use this tier when an issue is too big for one ticket but doesn't justify being promoted to a full Epic (e.g., a "Refactor X" parent with one sub-issue per file group).

`zh subissue` exposes the operations as subcommands. Sub-issue numbers are just regular issue numbers — there's nothing special about them in the API surface; only the parent/child relationship is.

```bash
# Link one or more issues as sub-issues of a parent (single API call)
zh subissue add 42 100 101 102

# List a parent's sub-issues (same format as 'zh epic show' children)
zh subissue list 42

# Unlink sub-issues — does NOT close them, just removes the parent relationship
zh subissue remove 42 100

# Reorder a sub-issue among its siblings. Note: sibling-anchored positions,
# NOT integer positions like 'zh reorder' uses.
zh subissue reorder 100 top
zh subissue reorder 100 bottom
zh subissue reorder 100 after 101
zh subissue reorder 100 before 102
```

`zh issue <N>` opportunistically surfaces parent/child info when present:

```
$ zh issue 100

#100 Refactor user-validation helpers

  State:     OPEN
  Pipeline:  In Progress
  ...
  Parent:    #42 Auth Service refactor pass
  Sub-issues: 3 (see 'zh subissue list 100')
  ZenHub:    https://app.zenhub.com/workspaces/.../issues/gh/acme/widget-service/100
  GitHub:    https://github.com/acme/widget-service/issues/100
```

**Sub-issue subcommands:**

| Subcommand | Description |
|------------|-------------|
| `add <parent#> <child#>...` | Link one or more issues as sub-issues of a parent (single API call) |
| `remove <parent#> <child#>...` | Unlink one or more sub-issues from a parent. Aliases: `rm` |
| `list <parent#>` | List a parent's sub-issues. Aliases: `ls` |
| `reorder <child#> <position>` | Reorder a sub-issue among its siblings. Position: `top`, `bottom`, `after <sibling#>`, `before <sibling#>`. Aliases: `order`, `pos` |

**Why sibling-anchored positions and not integers?** ZenHub's `reprioritizeSubIssue` mutation takes `afterId` / `beforeId` cursors (the IDs of sibling issues), not integer positions. The CLI mirrors that semantic directly so its behavior is predictable when called twice in a row — integer positions would silently compute against a moving list.

**Sub-issues vs Epics:** epics are workspace-scoped and visible in the workspace's Epics view; sub-issues are issue-scoped and only visible from their parent. Choose epics for cross-team / multi-sprint groupings; choose sub-issues for tight "one parent ticket, a few worker tickets" relationships.

> **Multi-repo workspaces:** `zh subissue` commands resolve issue numbers via the *current git checkout's* GitHub repo. In a ZenHub workspace that spans multiple GitHub repos, a parent in repo A with sub-issues in repo B can't be managed from a single working directory — each `zh subissue` invocation has to be run from a checkout of the repo whose issue numbers are being passed. The 3-tier framing (Epic → Issue → Sub-issue) often invites cross-repo grouping; plan parent/child placement with that limitation in mind, or do the cross-repo plumbing via the ZenHub web UI. Epic operations have the same scope limitation; sub-issues just feel it more often because the hierarchy is tighter.

### Sprints

ZenHub workspaces can have sprints with start/end dates, member assignments, and progress tracking (completed vs total points, closed issue count). `zh` exposes a read-only window into them — useful for "what's in the active sprint?" check-ins, retrospectives, and feeding sprint planning conversations.

```bash
# List open sprints (● marks the active sprint)
zh sprints

# Include closed sprints
zh sprints --all

# View active sprint details and issues
zh sprint current
zh sprint            # (also defaults to current)
zh sprint active     # alias for current

# View a specific sprint by name (case-insensitive)
zh sprint "Sprint 5"

# Compact output (no per-issue URLs)
zh sprint current --no-urls
```

Sample output:

```
Sprint: Sprint 7

  State:     OPEN
  Period:    2026-05-12 → 2026-05-26
  Points:    8/13 completed
  Closed:    3 issues

Issues (5):

  #100 │ acme/widgets │ 3 pts │ alice │ In Progress
    Add token rotation
    → https://github.com/acme/widgets/issues/100

  #101 │ acme/widgets │ 5 pts │ bob │ In Review
    ✓ Wire up refresh-token endpoint
    → https://github.com/acme/widgets/issues/101
```

Sprint membership mutations:

```bash
# Add one or more issues to a sprint
zh sprint add "Sprint 5" 42 43 44
zh sprint add current 42                # Active sprint
zh sa active 42                         # Top-level alias

# Remove one or more issues from a sprint
zh sprint remove "Sprint 5" 42
zh sprint rm current 42 43              # `rm` is an alias for `remove`
zh sr current 42                        # Top-level alias

# Move an issue between sprints
zh sprint remove "Sprint 4" 42
zh sprint add "Sprint 5" 42
```

The mutations report per-issue success / failure. An issue can fail to link for several reasons the API doesn't differentiate (already in the sprint, archived, otherwise ineligible) — `zh` surfaces the count and the affected issue numbers so you can investigate.

> Sprint functionality is inspired by the design proposed in [#2](https://github.com/daniel-pittman/zenhub-cli/pull/2) by [@jeremiahrose](https://github.com/jeremiahrose). The sprint queries (list / show / current) and mutations (add / remove) are credited to that design via `Co-Authored-By` trailers on the commits.

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

Issues assigned to acme-user (3):

  #98 │ acme/widget-service │ Product Backlog
    Fix login bug
    → https://app.zenhub.com/workspaces/.../issues/gh/acme/widget-service/98
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

# 2. Review backlog
zh pipeline "Product Backlog"

# 3. Move items to sprint
zh move 123 "TO DO"
zh move 456 "TO DO"

# 4. Prioritize the sprint
zh reorder 123 top
zh reorder 456 1

# 5. Set estimates
zh estimate 123 3
zh estimate 456 5

# 6. Assign work
zh assign 123 developer1
zh assign 456 developer2

# 7. Check specific issues
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

AI: [runs zh board]
    [runs zh pipeline "TO DO"]
    [runs zh pipeline "In Progress"]

    Summarizes work and blockers
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
- A `NO_ACCESS` error (e.g. on `create_issue`) usually means a lapsed ZenHub↔GitHub authorization — see the next entry

### "ZenHub can't see this repository" / lapsed ZenHub↔GitHub authorization
If `zh` reports it can't see a repo that clearly exists on GitHub, while reads of workspaces/boards keep working (and creates fail with `NO_ACCESS`), the cause is almost always that **ZenHub's GitHub authorization has lapsed**. ZenHub reaches GitHub repo objects through a GitHub grant that expires *separately* from your `zh` token, so the token still authenticates and lists workspaces while repo access is denied — which is easy to misread as a broken token or an unattached repo.

Fix: sign in at [app.zenhub.com](https://app.zenhub.com); if it prompts you to re-authorize GitHub, do so, then retry. If that doesn't resolve it, confirm the repo is attached to a workspace (ZenHub → workspace → Manage Repositories).

### "Issue not found"
The issue must exist in a repository that's part of your ZenHub workspace.

## MCP Server (for Claude Code / AI agents)

A Python MCP server (`mcp_server.py`) ships as a peer to the `zh` bash script. Most tools wrap `zh` over stdio for the human-facing read/write surface, while the sub-issue and sprint families call ZenHub's GraphQL API directly from Python (since v1.6.0) — sourcing structured data straight from the API rather than parsing terminal output. Any [Claude Code](https://docs.claude.com/en/docs/claude-code) session — or any other MCP-aware client — can drive ZenHub backlog operations as native MCP tools without shelling out.

### What it exposes

Roughly 35 tools covering the same surface as `zh`:

| Category | Tools |
|---|---|
| Read | `board`, `pipeline`, `pipelines`, `issue`, `mine`, `epic_list`, `epic_show`, `subissue_list`, `sprint_list`, `sprint_show`, `sprint_current`, `list_users`, `list_labels`, `list_types` |
| Issue lifecycle | `create_issue`, `close_issue`, `reopen_issue`, `move_issue`, `reorder_issue`, `comment`, `assign`, `unassign`, `set_estimate`, `set_priority` |
| Dependencies | `block_issue` |
| Epic management | `epic_create`, `epic_update`, `epic_add_children`, `epic_remove_children`, `epic_close`, `epic_reopen` |
| Sub-issue management | `subissue_add_children`, `subissue_remove_children`, `subissue_reorder` |
| Sprint membership | `sprint_add_issues`, `sprint_remove_issues` |
| Similarity search | `zh_similar`, `zh_reindex` (see below) |

`epic_delete` is intentionally NOT exposed as an MCP tool — permanent deletion is irreversible and should be invoked via the CLI directly with deliberation.

> **v1.6.0 architecture note:** the sub-issue family (`subissue_list`, `subissue_add_children`, `subissue_remove_children`, `subissue_reorder`) and the sprint family (`sprint_list`, `sprint_show`, `sprint_current`, `sprint_add_issues`, `sprint_remove_issues`) talk to ZenHub's GraphQL API directly from Python via `zh_api.py` + `zh_graphql_ops.py`, returning untruncated structured data with no text-parsing layer. Earlier versions (v1.5.x) shelled out to `zh --machine` and parsed TAB-separated streams; that contract was retired after four rounds of release-review findings caught a class of drift bugs (titles containing the visual separator, em-dash sentinel collisions, etc.). The remaining MCP tools still wrap `zh` because human-facing rendering already gives them everything they need.

### Similarity search (duplicate detection)

The MCP server includes a sentence-embedding-backed similarity index that finds existing issues semantically similar to a query — catching paraphrased duplicates that keyword search misses (e.g. *"Auth token refresh race condition under load"* and *"Users randomly logged out around 5pm"* score 0.6 cosine on the same underlying bug despite sharing zero keywords).

**Two tools + a `create_issue` pre-flight:**

- **`zh_similar(query, top_k=5, threshold=0.35)`** — ad-hoc similarity search against open issues. Always returns the top-K closest issues (never a bare empty list when issues exist); each carries a `meets_threshold` flag and a cosine score, and the response includes `any_above_threshold` for a quick strong-match read. Natural-language queries score higher than keyword salads. The cache auto-syncs on every call — no manual reindex needed.
- **`zh_reindex(full=False)`** — manually refresh the cache. Most callers don't need this; `zh_similar` auto-syncs on a 5-minute TTL.
- **`create_issue` pre-flight** — before creating an issue, the tool runs a similarity check on `title + body`. If any match exceeds the hard threshold (0.70 cosine), the create is **blocked** and the candidate matches are returned. Pass `confirm_create=True` to override after reviewing. Soft matches (0.55-0.70) are surfaced as warnings but don't block. (v1.9.1: the same pre-flight applies to `epic_create`, `initiative_create`, `project_create`, and `subtask_create`, so the planning-noun entry points share the duplicate-safety guarantee with `create_issue`.)
- **Structural-relative downgrade** *(v1.9.6)* — when you create a child under a parent (`parent=N`), a hard match against that parent is expected, not a duplicate: a parent whose body enumerates its children ("Sub-tasks: Wave A, Wave B, ...") scores 0.70-0.78 against the "Wave A" child you are wiring under it. Such a match is tagged `match_kind="structural_relative"` and downgraded from **block** to **warn** (the response sets `downgraded_structural=True`), so the `addSubIssues` wiring is not hard-blocked. Pass `related_issues=[...]` to extend the no-block set to known siblings in the same bulk-load. Genuine (non-structural) hard matches still block. `match_kind` is `"candidate"` for ordinary matches.

**How it works:**

1. **Model**: [`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) — 384-dim embeddings, ~80MB model, runs locally on CPU. Cached at `~/.cache/huggingface/` (persists across reboots).
2. **Cache**: pickled per-repo index at `~/.config/zh/index/<owner_repo>.pkl` (durable). Holds title + body preview + embedding for every open issue.
3. **Sync**: every query auto-checks the cache age. If > 5 min stale, calls `gh api repos/{owner}/{repo}/issues?since=<ISO8601>` to pull only changed issues and re-embeds those. After 7 days untouched, the cache rebuilds from scratch instead of trusting the delta.
4. **First run**: cold start downloads the model (~30s once) and pulls every open issue (~30-60s for a 100-issue backlog). After that, queries are millisecond-level.

**Tuning thresholds**: edit `similarity.py`'s `DUPLICATE_HARD_THRESHOLD` and `DUPLICATE_SOFT_THRESHOLD` constants. Calibration on the original test backlog:
- 0.75+ : identical title with different body → almost certainly a duplicate
- 0.60–0.70 : semantically related but distinct work → surface, don't block
- < 0.55 : just a topic neighbor → ignore

**Disabling**: pass `skip_duplicate_check=True` to `create_issue` (or any of the v1.9.1 planning-noun creates: `epic_create`, `initiative_create`, `project_create`, `subtask_create`) to bypass the pre-flight entirely (useful for bulk migrations).

### Installation

```bash
# 1. Make sure zh itself is installed and configured (see Configuration above).

# 2. Register the MCP server with Claude Code (user scope = all sessions on this machine):
claude mcp add --scope user zenhub \
    /usr/bin/python3 \
    /absolute/path/to/zenhub-cli/mcp_server.py

# 3. On first invocation, the server self-bootstraps a durable venv at
#    $XDG_DATA_HOME/zh/venv (default: ~/.local/share/zh/venv) and installs
#    mcp + sentence-transformers + numpy into it. Subsequent launches
#    validate the venv (pyvenv.cfg present, `import mcp` works, deps-hash
#    matches the current dependency tuple); if any check fails, the venv
#    is rebuilt automatically.
#
#    To force a clean rebuild: delete the venv directory shown in the
#    `[zenhub-mcp] bootstrapping <path> with ...` stderr line at startup,
#    then relaunch. The default is ~/.local/share/zh/venv but is overridden
#    by $ZH_MCP_VENV or $XDG_DATA_HOME; rely on the log line for the
#    actual path on your machine.

# 4. Verify:
claude mcp list
# Should show: zenhub: ... - ✓ Connected
```

### How tools resolve the GitHub repo

`zh` detects the GitHub repo from `git config --get remote.origin.url` in its working directory. The MCP server therefore resolves the repo as follows, in priority order:

1. Explicit `repo_path` argument on the tool call (absolute path of a git checkout)
2. `ZH_DEFAULT_REPO_PATH` environment variable
3. The MCP server's own working directory at launch

For multi-project use, the typical pattern is to pass `repo_path` explicitly on each tool call — one MCP server instance can drive multiple ZenHub workspaces, as long as each call points to a different git checkout.

### Environment overrides

| Variable | Purpose |
|---|---|
| `ZH_DEFAULT_REPO_PATH` | Default git checkout directory to run `zh` from when `repo_path` arg is omitted. |
| `ZH_BIN_PATH` | Path to the `zh` bash script (default: peer to `mcp_server.py`). Useful if you want to test against an alternate `zh` build. |
| `ZH_MCP_VENV` | Full **absolute** path of the venv the MCP server bootstraps and re-execs into. Overrides the default location. Useful for pinning to a project-local venv during development. Relative paths are rejected (they would resolve differently per launch cwd). |
| `XDG_DATA_HOME` | Standard XDG override for the data root. The venv lives at `$XDG_DATA_HOME/zh/venv` (default `~/.local/share/zh/venv`). |
| `ZH_MCP_PROBE_TIMEOUT` | Seconds for the per-launch `import` probe that validates the venv (default 30). Widen on slow media (NFS home, FileVault cold cache) where the import can otherwise time out and trigger a needless rebuild. |

### Requirements

- Python 3.10+ available on PATH (the server probes common locations: PATH default, Homebrew, pyenv shims, system Python).
- All the same requirements as `zh` itself (authenticated `gh` CLI, `ZH_TOKEN` configured, `jq`, `curl`).
- For the similarity-search tools: ~500MB of disk space the first time it runs — `sentence-transformers` installs `torch` + `transformers` into the MCP venv (~400MB) and the embedding model itself caches under `~/.cache/huggingface/` (~80MB).

### Optional: install the bundled `zenhub` agent for delegated use

The MCP server exposes the tools; the bundled **agent** (`agents/zenhub.md` in this repo) is the *behavioral layer* that makes a Claude Code session use those tools intelligently — proactive duplicate detection before drafting, propose-first protocol for destructive ops, batch audit-trail discipline, project-conventions discovery, and the three-option blocked-response framing for `create_issue` near-duplicates.

The bundled `agents/zenhub.md` is a deliberately-generic template — it ships with placeholder examples (`acme/widget-service`, epic `12345`, etc.) and no project-specific filing rules. After copying it to `~/.claude/agents/zenhub.md`, personalize your local copy with your project conventions, sprint patterns, and any non-generic context. **Anything you contribute back to this repo via PR should be genericized first** — local paths, real ticket titles, workspace IDs, and project names belong only in your personal `~/.claude/agents/` copy.

The agent is a single Markdown file with frontmatter. To install:

```bash
# 1. Copy the agent definition into user-scope agents.
mkdir -p ~/.claude/agents
cp agents/zenhub.md ~/.claude/agents/zenhub.md

# 2. Customize the "Project-specific conventions" section near the
#    bottom for your project(s) — filing rules, announcement channel,
#    active epics, sprint conventions. The file ships with a checklist
#    of what to capture per project.

# 3. (No restart needed.) In any Claude Code session on this machine
#    you can now delegate to it:
#
#    "Use the zenhub agent to file a ticket about <X>"
#    "Have the zenhub agent survey the board"
#    "Ask the zenhub agent to propose next sprint's tickets"
```

The agent description tells Claude Code's orchestrator when to delegate to it automatically (e.g. when the user mentions ZenHub ticket operations). You don't have to invoke it by name.

What the agent enforces on top of the raw tools:

| Behavior | Without agent | With agent |
|---|---|---|
| **Duplicate check** | Only fires if you call `zh_similar` manually or if `create_issue` pre-flight catches it | Always runs `zh_similar` *before* drafting; surfaces matches; presents 3-option decision on blocked responses |
| **Destructive ops** | Fired immediately when invoked | Propose-first protocol with what / why / new-state / undo |
| **Batch ops (>5 writes)** | No audit trail unless you build one | Audit-trail YAML required; per-batch announce + spot-check pauses |
| **Project conventions** | Re-discovered each session | Read from project memory or `CLAUDE.md` before any write op |
| **`Closes #N` hazard** | Silent (commit message lands and 10 unrelated tickets auto-close) | Hard rule against this pattern; alternate notations enforced |
| **Filing destination** | Has to be specified per ticket | Defaults from project conventions |

The agent file is genuinely portable — strip the example "Project-specific conventions" section and you have a clean template that works for any ZenHub-using project.

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow; the essentials:

- Branch off `develop` (the default branch); `main` only receives release PRs.
- Run the test suite in a virtualenv: `python -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests/`.
- Every PR runs the `syntax` checks (bash + Python + pytest across 3.10/3.11/3.12), a Semgrep scan, and an automated Claude review whose verdict drives the `review-gate` merge check.

Security issues go through [`SECURITY.md`](SECURITY.md), not the public tracker.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built for use with [ZenHub](https://www.zenhub.com/)
- Inspired by the need for efficient terminal-based project management
- Designed with AI-assisted workflows in mind
