# GitHub CLI (`gh`)

Required by both GitHub plugins and by `gh-llm` and `gh-slate`. It provides GitHub authentication and REST/GraphQL operations.

## Prerequisites and installation

Use the [official installation instructions](https://github.com/cli/cli#installation). You need connectivity to the target GitHub host and an account or token with access to the configured repositories.

For an interactive account setup:

```bash
gh --version
gh auth login
gh auth status
```

For service credentials, configure `integrations.github` as described in [Nyanpasu configuration](../README.md#configuration). Agent subprocesses need the same intended identity through the selected backend's `env` or `pass_env`; plugin authentication alone does not configure the agent's shell tools.

Review publication needs appropriate PR/comment permissions. PR maker additionally needs push and PR creation permissions. For HTTPS Git authentication, see [`gh auth setup-git`](https://cli.github.com/manual/gh_auth_setup-git); SSH remotes use SSH credentials.
