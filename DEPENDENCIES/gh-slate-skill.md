# gh-slate skill

Required by the current reviewer instructions. It describes the dashboard inspection, preview, revision-aware update, and verification workflow.

## Prerequisites and installation

Install [gh-slate](gh-slate.md) and authenticated [gh](github-cli.md), then install the skill for the selected agent:

```bash
npx skills add https://github.com/ShigureLab/gh-slate --skill gh-slate
```

This method needs [Node.js/npm](nodejs.md). Use the service account's user/global skill scope; see [skill management](skill-management.md).

The [upstream skill installation guide](https://github.com/ShigureLab/gh-slate#install-the-agent-skill-separately) also documents installation through versions of `gh` that support `gh skill`. The CLI, skill, and Nyanpasu's bundled dashboard profile are distinct components.
