# gh-llm

Required by the current GitHub reviewer instructions for structured PR context, pinned review locations, threads, and review submission.

## Prerequisites and installation

Requires [Python](python.md), [uv](uv.md) for this installation method, and authenticated [gh meeting the reviewer minimum version](github-cli.md#minimum-version). The current upstream Python minimum is documented in [requirements](https://github.com/ShigureLab/gh-llm#requirements).

```bash
uv tool install --python 3.14 gh-llm
gh-llm --version
```

Also install the separate [github-conversation skill](github-conversation.md). Verify repository access with a read of a real PR, following the [upstream quick start](https://github.com/ShigureLab/gh-llm#quick-start).

The [upstream installation guide](https://github.com/ShigureLab/gh-llm#install) also offers a `gh llm` extension. The standalone executable above matches Nyanpasu's default `plugins.settings.github_reviewer.gh_llm_bin = "gh-llm"`.
