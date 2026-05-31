# nyanpasu-github

Shared GitHub support package for Nyanpasu plugins.

This package is intentionally not part of the Nyanpasu core runtime. It contains reusable GitHub-side helpers for plugins, including repo configuration models, task instruction document resolution, workspace references, webhook signature verification, `gh` command wrappers, deterministic pull request publishing, and small agent task helpers for GitHub plugins.

Shared GitHub settings are read from the generic core table:

```toml
[integrations.github]
token_env = "NYANPASU_GITHUB_TOKEN"
git_author_name = "Nyanpasu"
git_author_email = "nyanpasu@example.invalid"
```

`token_env` is preferred. `token` is also supported for controlled environments, but it places the secret in TOML. When neither is set, helpers use the ambient `gh auth` state.

Agent-driven plugins may ask Codex to run `gh` itself. In that case, also expose the token variable to Codex:

```toml
[codex]
pass_env = ["NYANPASU_GITHUB_TOKEN"]
```

Shared helpers provide prompt-facing authentication notes that mention only environment variable names, never token values.

`nyanpasu_github.agent_tasks` is the shared layer for GitHub plugins that hand GitHub writes to Codex. It resolves configured repos and base branches, creates branch workspaces and task instruction documents, builds simple branch-backed `AgentTask` objects, and parses final `PR: <url>` / `NO_PR: <reason>` markers. It deliberately does not define product-specific prompts or plugin state machines.
