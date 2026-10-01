# Additional agent integrations

Optional. Nyanpasu's standard reviewer uses the selected agent CLI, GitHub tools, and reviewer skills. Additional integrations depend on the repositories and services you want it to work with.

## Prerequisites and installation

| Integration                                  | Prerequisites                                                            | Installation/setup                                                                                                                                                                               |
| -------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Project-specific skills                      | Selected agent and whatever tools the skill invokes                      | Follow the publisher's `SKILL.md` and [skill management](skill-management.md)                                                                                                                    |
| Documentation, browser, or other MCP servers | A backend supporting the chosen server, its runtime, and any credentials | Follow the server publisher's guide and [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) or [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp) |

Install and authenticate integrations for the backend account that runs tasks. Tools available in a separate desktop chat are not automatically available to Nyanpasu's agent subprocesses.
