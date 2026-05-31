# nyanpasu-github-pr-maker

GitHub pull request creation plugin for Nyanpasu.

It accepts a task request, asks Codex to implement it in a managed worktree, then publishes a branch and creates a pull request from the post-process hook.

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
git_author_name = "Nyanpasu"
git_author_email = "nyanpasu@example.invalid"

[plugins.github_pr_maker.repos."owner/repo"]
local_path = "/path/to/repo"
github_remote = "git@github.com:owner/repo.git"
base_branches = ["main"]
```

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

The response contains the accepted Nyanpasu task id. After Codex finishes, the post-process hook creates the pull request unless `dry_run = true` or no changes were produced.

## Follow-up

When `follow_up_enabled = true`, the plugin records PRs it created and polls them every `follow_up_interval_seconds`. If the PR receives actionable state changes such as new comments, reviews, head updates, or failing checks, it submits a follow-up task with the same Nyanpasu `context_key` and the same PR branch workspace. The core runtime serializes that context, so follow-up work does not fork the Codex thread or write the same worktree concurrently.

Follow-up post-processing pushes new commits to the existing PR branch. It does not open another pull request.
