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

Agent tasks execute in managed workspaces with isolated homes. Install the required skills into each backend’s curated `home.template`, using its native discovery layout and real files rather than symbolic links. Alternatively, explicitly grant a shared skill directory and configure the native CLI to discover it. Installing only into the Nyanpasu source checkout or the service account’s personal home does not make the skills available to workers.

Confirm the required skill names are discoverable in an actual Nyanpasu task. Workers use isolated native homes, so discovery in the service account’s normal interactive CLI is insufficient. Configure explicit tool/skill directories with [execution isolation](../docs/configuration.md#linux-execution-isolation) and the native CLI’s discovery settings. A read-only path grant alone does not register a skill.

The [Skills directory](https://www.skills.sh/docs) is one public discovery option. Follow each skill publisher's installation instructions for its prerequisites.
