# GitHub CLI (`gh`)

Required by both GitHub plugins and by `gh-llm` and `gh-slate`. It provides GitHub authentication and REST/GraphQL operations.

## Minimum version

The current GitHub reviewer workflow requires **GitHub CLI 2.57.0 or newer**:

- `gh api --paginate --slurp`, used by `gh-llm` and `gh-slate`, requires [2.48.0](https://github.com/cli/cli/releases/tag/v2.48.0).
- `gh auth status --active`, used by both tools' authentication checks, requires [2.57.0](https://github.com/cli/cli/releases/tag/v2.57.0).

Check the executable on the service and agent subprocesses' `PATH`, inside the container when using one. A newer host installation does not satisfy the container's dependency. Distribution packages can be too old: Ubuntu 24.04's `gh` 2.45.0 lacks both flags.

## Prerequisites and installation

Use the [official installation instructions](https://github.com/cli/cli#installation) to install a version meeting the minimum above. For Debian/Ubuntu, use the [GitHub CLI package repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md#debian-ubuntu-linux-raspberry-pi-os-apt), or an official release binary, if the distribution package is too old. You need connectivity to the target GitHub host and an account or token with access to the configured repositories.

For an interactive account setup:

```bash
gh --version
gh auth login
gh auth status --active
```

Before enabling GitHub reviews, verify the installed tools with `gh-llm pr view NUMBER --repo OWNER/REPO` on an accessible PR and `gh-slate doctor --json`. A successful `gh --version` or basic `gh api` request alone does not check the flags required by these tools.

For service credentials, configure `integrations.github` as described in [Nyanpasu configuration](../README.md#configuration). Agent subprocesses need the same intended identity through the selected backend's `env` or `pass_env`; plugin authentication alone does not configure the agent's shell tools.

Review publication needs appropriate PR/comment permissions. PR maker additionally needs push and PR creation permissions. For HTTPS Git authentication, see [`gh auth setup-git`](https://cli.github.com/manual/gh_auth_setup-git); SSH remotes use SSH credentials.
