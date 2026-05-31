# nyanpasu-github-reviewer

GitHub pull request review plugin for Nyanpasu.

This package owns GitHub-specific behavior: webhook payload parsing, polling, PR state baselines, `gh-llm` review prompts, and GitHub-facing review policy. The Nyanpasu core runtime only receives generic `AgentTask` objects.

Each PR maps to one Nyanpasu context key, so follow-up events reuse the same Codex thread and context worktree. The worktree is reset to the current PR head before each review task.

## Config

```toml
enabled_plugins = ["github_reviewer"]

[plugins.github_reviewer]
github_login = "your-github-login"
review_language = "Chinese"
poll_enabled = true
poll_interval_seconds = 600
poll_event_pages = 3
poll_max_events_per_cycle = 0
dry_run = false
post_reviews = true

[[plugins.github_reviewer.instruction_docs]]
name = "SOUL.md"
path = "/path/to/SOUL.md"

[plugins.github_reviewer.repos."owner/repo"]
local_path = "/path/to/repo"
github_remote = "https://github.com/owner/repo.git"
base_branches = ["main"]

[[plugins.github_reviewer.repos."owner/repo".instruction_docs]]
name = "AGENTS.md"
path = "/path/to/repo/AGENTS.md"
required = false
```

`instruction_docs` are resolved when a PR event becomes an `AgentTask`. Plugin-level documents apply to every reviewer task; repo-level documents are appended for that repo. This is how `SOUL.md`, `AGENTS.md`, and similar task-local policy files reach Codex without making them global GitHub-reviewer prompt text.

## Run

When installed as a Nyanpasu plugin, `nyanpasu serve` starts the plugin and its poller if `poll_enabled = true`.

```bash
export NYANPASU_HOME="$HOME/.nyanpasu"
uv run nyanpasu serve
```

Nyanpasu always reads `$NYANPASU_HOME/config.toml`, defaulting to `~/.nyanpasu/config.toml`.

Webhook endpoint:

```text
POST /plugins/github-reviewer/webhook
```

Manual plugin commands:

```bash
uv run nyanpasu-github-reviewer poll --once
uv run nyanpasu-github-reviewer poll
uv run nyanpasu-github-reviewer review owner/repo 123
```

The poller uses GitHub repository events. The first run records the current repo event cursor and does not process older events; later runs process filtered events after that cursor. Already processed delivery ids are skipped. `poll_max_events_per_cycle = 0` processes every matching event in the poll window; a positive value is an explicit cap.
