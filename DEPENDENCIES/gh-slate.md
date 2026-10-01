# gh-slate

Required by the current GitHub reviewer workflow to maintain the `nyanpasu-review` dashboard comment on each PR. This is separate from Nyanpasu's local Web Dashboard.

## Prerequisites and installation

Requires [Python](python.md), [uv](uv.md) for this installation method, and authenticated [gh](github-cli.md). Use a version meeting the [reviewer template's minimum](../packages/nyanpasu-github-reviewer/README.md#review-dashboard).

Install the [published CLI](https://pypi.org/project/gh-slate/) and verify it:

```bash
uv tool install --python 3.14 gh-slate
gh-slate --version
gh-slate doctor --json
```

Also install the separate [gh-slate skill](gh-slate-skill.md). For source installation and the alternative `gh slate` extension, see the [upstream installation guide](https://github.com/ShigureLab/gh-slate#install).

The reviewer package supplies `boards.toml`, the schema, and the Markdown template. Follow its [dashboard setup and preview instructions](../packages/nyanpasu-github-reviewer/README.md#review-dashboard); no additional template download is needed.
