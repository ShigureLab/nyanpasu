# Skill installation and discovery

Skill management tools are optional setup helpers. The [reviewer's required skills](index.md#agent-skills) still need to be available to whichever backend runs its tasks.

## Prerequisites and installation

Choose an installation method supported by your agent:

| Method                     | Prerequisites                                            | Installation instructions                                                                                                   |
| -------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `npx skills add`           | [Node.js/npm](nodejs.md), access to the skill repository | [Skills CLI documentation](https://www.skills.sh/docs/cli); specific commands appear on each skill page                     |
| Codex Skill Installer      | [Codex](codex.md)                                        | Invoke `$skill-installer` to install from a repository; [official skill guide](https://learn.chatgpt.com/docs/build-skills) |
| Codex Skill Creator        | [Codex](codex.md)                                        | Use the bundled `$skill-creator` for custom skills; [official skill guide](https://learn.chatgpt.com/docs/build-skills)     |
| Claude Code skills/plugins | [Claude Code](claude-code.md)                            | Follow [Claude's skill documentation](https://code.claude.com/docs/en/skills) and the skill publisher's installer           |
| `gh skill`                 | A [gh](github-cli.md) version supporting that command    | Follow the skill publisher's instructions, such as [gh-slate](gh-slate-skill.md)                                            |

Use user/global scope for the service account, rather than installing only into the Nyanpasu source checkout: the agent executes in managed task workspaces. Consult the selected CLI's current documentation for its discovery directories. If switching backends, install for the new backend too, or use a shared directory through supported symlinks.

Start the selected CLI as the service account and confirm the required skill names are discoverable. A skill directory on disk does not establish that the running backend loads it.

The [Skills directory](https://www.skills.sh/docs) is one public discovery option. Follow each skill publisher's installation instructions for its prerequisites.
