# gh-stack and its skill

Optional for creating and maintaining stacked PRs. Nyanpasu's reviewer reads stack metadata from GitHub's API and does not require this CLI or skill; see [stacked PR support](../packages/nyanpasu-github-reviewer/README.md#stacked-pull-requests).

## Prerequisites and installation

Requires [Git](git.md), authenticated [gh](github-cli.md), and repository permissions suitable for the stack operations you intend to perform. Follow the [official installation instructions](https://github.com/github/gh-stack#installation):

```bash
gh extension install github/gh-stack
gh stack --help
```

For agents that author stacks, the same repository documents an [optional gh-stack skill](https://github.com/github/gh-stack#ai-agent-integration). Install it through the upstream instructions with a `gh` version supporting skill installation. Adding it does not change the Nyanpasu reviewer's publication boundaries.
