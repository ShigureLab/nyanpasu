# Codex CLI

Required when `runtime.backend = "codex"`. [Claude Code](claude-code.md) is the other supported backend; installing both is optional.

## Prerequisites and installation

Follow the [official Codex CLI installation guide](https://learn.chatgpt.com/docs/codex/cli) for your OS, then complete [authentication](https://learn.chatgpt.com/docs/auth) as the account that will run Nyanpasu. You need network access and an account/provider that can use the configured model. A standalone installation does not require Node.js; an npm installation needs Node.js/npm.

On Linux and WSL2, also install [bubblewrap](bubblewrap.md) and verify sandboxed command execution. A successful model response or `codex --version` alone does not establish that shell tools can run.

Verify the executable and complete a small read-only task before enabling the service:

```bash
codex --version
codex
```

Nyanpasu uses Codex's app-server interface. Select a CLI supporting the runtime settings in [config.py](../src/nyanpasu/config.py) and the interface used by [codex.py](../src/nyanpasu/codex.py), including automatic permission review. Configure `codex.bin` when the executable is not on the service's `PATH`.

Install the [reviewer skills](index.md#agent-skills) for this backend and configure credentials through `codex.env` or `codex.pass_env`. See [runtime configuration](../README.md#runtime-configuration); retain the backend's native history so the Dashboard can read earlier sessions.
