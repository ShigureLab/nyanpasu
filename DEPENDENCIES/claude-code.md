# Claude Code

Required when `runtime.backend = "claude"`. [Codex](codex.md) is the other supported backend; installing both is optional.

## Prerequisites and installation

Use the [official setup guide](https://code.claude.com/docs/en/setup) for supported platforms, installers, and authentication. You need network access and a supported account or API provider with access to the configured model. The native installation does not require Node.js; the npm installation has its own Node.js requirements.

Verify the executable, then sign in and complete a small read-only task as the service account:

```bash
claude --version
claude
```

Nyanpasu requires a CLI compatible with its stream output, session resume, and configured permission mode. The default is `permission_mode = "auto"`, which also needs account/model support. See [runtime configuration](../README.md#runtime-configuration) for compatibility, default CLI arguments, and fallback settings.

Install the [reviewer skills](index.md#agent-skills) for Claude and configure credentials through `claude.env` or `claude.pass_env`. Set `claude.bin` for a custom executable. Preserve its configuration and native session history across restarts.
