# Claude Code

Required when a task selects a configured backend whose driver is `claude-code`. [Codex](codex.md) is the other supported backend; installing both is optional.

## Prerequisites and installation

Use the [official setup guide](https://code.claude.com/docs/en/setup) for supported platforms, installers, and authentication. You need network access and a supported account or API provider with access to the configured model. The native installation does not require Node.js; the npm installation has its own Node.js requirements.

Verify the executable, then sign in and complete a small read-only task as the service account:

```bash
claude --version
claude
```

Nyanpasu requires a CLI compatible with its stream output, session resume, and configured permission mode. The default is `permission_mode = "auto"`, which also needs account/model support. See the [configuration example](../examples/config.toml) for default CLI arguments and [runtime configuration](../README.md#runtime-configuration) for fallback settings.

Install the [reviewer skills](index.md#agent-skills) for Claude and configure credentials through `backends.claude.process.env` or `backends.claude.process.pass_env`. Set `backends.claude.process.command` for a custom executable. Preserve its configuration and native session history across restarts.

Configure the required skills through [native session homes](../README.md#native-session-homes) and the native CLI’s discovery settings. Verify discovery inside a Nyanpasu task; a normal interactive CLI session uses a different home.
