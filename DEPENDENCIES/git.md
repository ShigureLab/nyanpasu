# Git

Required for tasks that use a repository. Nyanpasu prepares workspaces and pins revisions with Git; reviewers use it to inspect diffs, and PR maker uses it to commit and push.

## Prerequisites and installation

Install a package or binary for your OS using the [official Git installation page](https://git-scm.com/install/). Verify:

```bash
git --version
```

The service account needs read access to configured local repositories and authentication for their remotes. Use HTTPS credentials or SSH according to the remote URL. For GitHub, see [GitHub CLI](github-cli.md).

PR maker also needs a Git author identity and push access. Nyanpasu's `integrations.github.git_author_name` and `git_author_email` settings provide commit identity guidance; see the [configuration example](../examples/config.toml).
