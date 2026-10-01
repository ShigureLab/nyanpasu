# Dependencies

This directory covers the tools and skills used to install, run, and develop Nyanpasu. Each page explains when a dependency is needed, its prerequisites, and how to install it.

**Required** means needed for the stated workflow. **Choose one** means alternatives, not cumulative requirements. **Optional** means Nyanpasu can run without it.

Package versions and complete transitive dependencies remain authoritative in [pyproject.toml](../pyproject.toml), the plugin manifests, [uv.lock](../uv.lock), [package.json](../package.json), and [pnpm-lock.yaml](../pnpm-lock.yaml). Managed libraries are documented together with the environment that installs them.

## Runtime and GitHub plugins

| Dependency                                                    | Required?                                         | Used for                                                     |
| ------------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------ |
| [Python](python.md)                                           | Required                                          | Service and Python CLI runtime                               |
| [uv](uv.md)                                                   | Required for the documented source installation   | Python, workspace dependencies, and standalone tools         |
| [Git](git.md)                                                 | Required for repository tasks                     | Clones, workspaces, revisions, and diffs                     |
| [Nyanpasu packages and Python runtime libraries](nyanpasu.md) | Core required; plugins depend on enabled features | Service, GitHub helpers, reviewer, and optional PR maker     |
| [Codex CLI](codex.md)                                         | Choose one backend                                | Agent execution through Codex                                |
| [bubblewrap](bubblewrap.md)                                   | Required for the current Codex Linux sandbox      | Sandboxed shell execution on Linux and WSL2                  |
| [Claude Code](claude-code.md)                                 | Choose one backend                                | Agent execution through Claude Code                          |
| [GitHub CLI (`gh`)](github-cli.md)                            | Required for either GitHub plugin                 | GitHub authentication and API operations                     |
| [gh-llm](gh-llm.md)                                           | Required by the current reviewer workflow         | PR context, review locations, threads, and review submission |
| [gh-slate](gh-slate.md)                                       | Required by the current reviewer workflow         | The `nyanpasu-review` GitHub dashboard comment               |
| [SQLite](sqlite.md)                                           | Python module required; CLI optional              | Persistent task state and optional manual diagnostics        |

## Agent skills

Skills contain instructions; their command-line tools are separate installations. Install skills for the selected backend under the OS account that runs Nyanpasu. See [skill installation and discovery](skill-management.md) for user scope and backend differences.

| Skill                                                                        | Required?                                 | Tool prerequisites                       |
| ---------------------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------- |
| [github-conversation](github-conversation.md)                                | Required by the current reviewer workflow | `gh-llm`, authenticated `gh`             |
| [gh-slate](gh-slate-skill.md)                                                | Required by the current reviewer workflow | `gh-slate`, authenticated `gh`           |
| [ast-grep](ast-grep-skill.md)                                                | Optional                                  | `ast-grep` CLI                           |
| [Skill Installer, Skill Creator, and discovery helpers](skill-management.md) | Optional setup/authoring helpers          | Selected agent; installer-specific tools |
| [gh-stack skill](gh-stack.md)                                                | Optional for authoring stacks             | `gh stack`                               |
| [Project-specific skills and MCP integrations](agent-integrations.md)        | Optional                                  | Depends on the project or integration    |

Reviewer roles, output rules, and the dashboard profile ship with [nyanpasu-github-reviewer](../packages/nyanpasu-github-reviewer/README.md). `SOUL.md`, `AGENTS.md`, and other configured `instruction_docs` are policy documents, not additional skill packages.

## Dashboard and development

| Dependency                                        | Required?                                         | Used for                                                          |
| ------------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------- |
| [Node.js](nodejs.md)                              | Required to build/develop the Dashboard           | JavaScript build tools; optional npm-based installers             |
| [pnpm](pnpm.md)                                   | Required to build/develop the Dashboard           | Install the locked frontend dependencies                          |
| [Frontend libraries and tooling](frontend.md)     | Required to build/develop the Dashboard           | React, Markdown rendering, Vite+, TypeScript, and frontend checks |
| [Python development tools](python-development.md) | Required for the corresponding development checks | pytest, Ruff, ty, HTTP test client, and rerun support             |
| [Playwright and Chromium](playwright.md)          | Required for browser interaction tests            | Dashboard end-to-end tests                                        |
| [just](just.md)                                   | Optional locally; used by CI recipes              | Shortcuts for install, checks, tests, and builds                  |

A source checkout needs a Dashboard build before serving `/dashboard`. A distribution that already includes those assets does not need Node.js or pnpm merely to serve them.

## Optional agent and diagnostic tools

| Dependency                                               | Required?                                   | Used for                                                  |
| -------------------------------------------------------- | ------------------------------------------- | --------------------------------------------------------- |
| [ripgrep (`rg`)](ripgrep.md)                             | Optional, recommended                       | Fast text and file searches                               |
| [ast-grep CLI](ast-grep.md)                              | Optional                                    | Structural code search                                    |
| [curl](curl.md)                                          | Optional                                    | HTTP diagnostics and some upstream installers             |
| [jq](jq.md)                                              | Optional                                    | Inspect JSON responses and logs                           |
| [gh-stack](gh-stack.md)                                  | Optional                                    | Authoring/managing stacked PRs; not needed to review them |
| [Target repository environments](target-environments.md) | Required only for the target's tests/builds | Language SDKs, compilers, services, and hardware          |

## Installation order

1. Install [uv](uv.md), [Python](python.md), and [Git](git.md), then install the [Nyanpasu workspace](nyanpasu.md).
2. Install and authenticate [Codex](codex.md) or [Claude Code](claude-code.md).
3. Install [Node.js](nodejs.md) if building the Dashboard or using npm/npx installers. For the Dashboard, also install [pnpm](pnpm.md) and [build the frontend](frontend.md).
4. For GitHub review, install [gh](github-cli.md), [gh-llm](gh-llm.md), [gh-slate](gh-slate.md), and both required [reviewer skills](#agent-skills).
5. Create the service configuration from [examples/config.toml](../examples/config.toml), replacing placeholder paths and identities. Follow [configuration](../README.md#configuration) and [runtime configuration](../README.md#runtime-configuration) for credentials, backend selection, and enabled plugins, then [start the service](../README.md#run).

Install executables on the service's `PATH`; an interactive shell alias is not an executable. Plugin-side GitHub credentials and agent child-process credentials must both be configured. Keep `NYANPASU_HOME` and the selected backend's native history/configuration persistent across restarts.
