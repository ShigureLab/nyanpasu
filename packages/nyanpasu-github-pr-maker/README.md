# nyanpasu-github-pr-maker

GitHub pull request creation plugin for Nyanpasu.

It accepts a task request, prepares a concrete pull request plan, and asks Codex to implement the change, commit, push, and create the PR with `gh pr create` in the managed worktree. The post-process hook only records the PR URL or no-op reason reported by the agent.

## Config

```toml
enabled_plugins = ["github_pr_maker"]

[plugins.github_pr_maker]
branch_prefix = "nyanpasu"
default_base_branch = "main"
dry_run = false
draft = false
follow_up_enabled = true
follow_up_interval_seconds = 600

[integrations.github]
token_env = "NYANPASU_GITHUB_TOKEN"
git_author_name = "Nyanpasu"
git_author_email = "nyanpasu@example.invalid"

[codex]
pass_env = ["NYANPASU_GITHUB_TOKEN"]

[plugins.github_pr_maker.repos."owner/repo"]
local_path = "/path/to/repo"
github_remote = "git@github.com:owner/repo.git"
base_branches = ["main"]
```

`[integrations.github]` is provided by `nyanpasu-github`, not by the core runtime. `token_env` lets plugin-side `gh` helpers run without relying on global `gh auth`; if no token is configured, they fall back to ambient `gh` authentication state.

PR creation itself is agent-driven: Codex runs `git` and `gh` inside the worktree. If you use `token_env`, expose the same variable to Codex with `codex.pass_env`. Nyanpasu stores the variable name in prompts and task metadata, not the token value. `git_author_*` is optional guidance for commits created by the agent.

## API

```http
POST /plugins/github-pr-maker/tasks
```

```json
{
  "repo": "owner/repo",
  "task": "Implement the requested change and update tests.",
  "base_branch": "main",
  "title": "Implement requested change"
}
```

The response contains the accepted Nyanpasu task id. After Codex finishes, post-processing records:

- `published` when the final message contains `PR: https://github.com/.../pull/<number>`.
- `no_changes` when the final message contains `NO_PR: <reason>`.
- `dry_run` when the task was configured as a dry run.
- `failed` when Codex completed but did not report a PR URL or no-op reason.

## Follow-up

When `follow_up_enabled = true`, the plugin records PRs it created and polls them every `follow_up_interval_seconds`. If the PR receives actionable state changes such as new comments, reviews, head updates, or failing checks, it submits a follow-up task with the same Nyanpasu `context_key` and the same PR branch workspace. The core runtime serializes that context, so follow-up work does not fork the Codex thread or write the same worktree concurrently.

Follow-up tasks ask Codex to push new commits to the existing PR branch. They do not open another pull request.
