# github-conversation skill

Required by the current reviewer instructions. It teaches the agent to read GitHub conversations, work with review threads, and publish evidence-backed responses.

## Prerequisites and installation

Install [gh-llm](gh-llm.md) and authenticated [gh](github-cli.md), then install the skill for the selected agent:

```bash
npx skills add https://github.com/ShigureLab/gh-llm --skill github-conversation
```

This method needs [Node.js/npm](nodejs.md). Select the service account's agent and user/global scope so managed task workspaces can discover it. See [skill management](skill-management.md) for alternatives and discovery verification.

The source and update instructions live in the [upstream repository](https://github.com/ShigureLab/gh-llm#install-skill). Installing the CLI does not install this skill.
