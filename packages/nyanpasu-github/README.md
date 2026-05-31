# nyanpasu-github

Shared GitHub support package for Nyanpasu plugins.

This package is intentionally not part of the Nyanpasu core runtime. It contains reusable GitHub-side helpers for plugins, including repo configuration models, task instruction document resolution, workspace references, webhook signature verification, `gh` command wrappers, and deterministic pull request publishing.

Shared GitHub settings are read from the generic core table:

```toml
[integrations.github]
token_env = "NYANPASU_GITHUB_TOKEN"
git_author_name = "Nyanpasu"
git_author_email = "nyanpasu@example.invalid"
```

`token_env` is preferred. `token` is also supported for controlled environments, but it places the secret in TOML. When neither is set, helpers use the ambient `gh auth` state.
