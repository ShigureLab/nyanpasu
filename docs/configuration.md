# Configuration and execution targets

Nyanpasu reads `$NYANPASU_HOME/config.toml`; `NYANPASU_HOME` defaults to `~/.nyanpasu`. Configuration, SQLite state, managed workspaces, isolated native homes, and memory live below that directory. `state_dir` is not configurable in TOML.

Start from [examples/config.toml](../examples/config.toml). The configuration separates responsibilities:

| Section                    | Responsibility                                               | Default                                                                  |
| -------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------ |
| `backends.<name>.driver`   | Native adapter                                               | `codex` or `claude-code`; explicitly declare it for a configured backend |
| `backends.<name>.process`  | Literal command, environment, and forwarded variables        | Driver command; empty `env` and `pass_env`                               |
| `backends.<name>.home`     | Isolated native directory and optional curated home template | Driver directory; no template                                            |
| `backends.<name>.defaults` | Backend's model and reasoning preferences                    | Both unspecified                                                         |
| `backends.<name>.options`  | Adapter-specific permission and protocol settings            | See below                                                                |
| `tasks.defaults.execution` | Default task target                                          | `backend = "codex"`                                                      |
| `tasks.defaults.limits`    | Default execution limits                                     | `turn_timeout_seconds = 3600`                                            |
| `tasks.kinds.<kind>`       | Execution and limits for one task kind                       | Inherit applicable defaults                                              |
| `runtime`                  | Scheduling and workspace cleanup                             | Four root slots; 600-second coalescing window                            |
| `isolation.readonly_paths` | Explicit read-only grants for required tools and resources   | Empty                                                                    |
| `memory`                   | Shared memory availability and consolidation                 | Enabled; consolidation enabled; at most 10 search results                |
| `plugins.enabled`          | Plugins to start                                             | Empty                                                                    |
| `plugins.settings.<id>`    | One plugin's configuration                                   | Empty                                                                    |
| `integrations.<id>`        | Shared integration configuration                             | Empty                                                                    |

An empty configuration registers `codex` and `claude` backends. A supplied `backends` table defines its own registry. Names identify configurations, so two names may use the same driver with different credentials, defaults, or options. Install every driver that admitted work needs, including queued tasks recovered after a restart.

## Backend processes

`process.command` is an argument array, passed directly without a shell. Defaults are `["codex"]` and `["claude", "--permission-prompts", "none", "--system-prompt-snapshot", "off"]`. An explicit command replaces the entire array. Supplying only `process.env` or `process.pass_env` retains the driver command.

```toml
[backends.review]
driver = "claude-code"

[backends.review.process]
command = ["/opt/agents/claude", "--permission-prompts", "none", "--system-prompt-snapshot", "off"]
pass_env = ["ANTHROPIC_API_KEY"]

[backends.review.process.env]
GH_PROMPT_DISABLED = "1"
GH_TOKEN = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }

[backends.review.defaults]
model = "your-review-model"
reasoning = "high"

[tasks.defaults.execution]
backend = "review"
```

`env` overrides inherited values and `pass_env`; its keys do not also need to appear in `pass_env`. Environment commands run with the service environment and Nyanpasu home as their working directory. They cannot refer to other configured `env` entries. Output must be nonempty UTF-8 without NUL; trailing CR/LF is removed. Environment commands are resolved when a backend is constructed for a turn or history reader. A nonzero exit or 10-second timeout prevents that operation, and errors omit the command's output.

Plugin-side GitHub credentials remain under `integrations.github`. Agent-side `gh` commands need credentials in the selected backend's process environment as well. Authentication guidance names variables without including their values.

Codex options default to `sandbox = "workspace-write"`, `approval_policy = "on-request"`, `approvals_reviewer = "auto_review"`, and `rpc_timeout_seconds = 60`. The RPC deadline is separate from a task's turn timeout. Claude options default to `permission_mode = "auto"`, no `allowed_tools`, and no explicit `fallback_models`.

Claude's optional fallback chain belongs to its adapter:

```toml
[backends.claude]
driver = "claude-code"

[backends.claude.defaults]
model = "your-primary-model"
reasoning = "high"

[backends.claude.options]
fallback_models = [
  { model = "your-backup-model", reasoning = "medium" },
  "your-last-model",
]

[tasks.defaults.execution]
backend = "claude"
```

At most three fallbacks are allowed. Omitted fallback reasoning inherits the selected task's reasoning. Per-model fallback settings support `low`, `medium`, `high`, and `xhigh`, subject to the provider's capabilities. Other model/reasoning support is validated by the native CLI, without silently changing the requested value. Generation fallback uses Claude's native chain. If auto review specifically reports an unavailable classifier, Nyanpasu resumes the same session with the next configured model and retains auto review. Ordinary safety rejections do not switch models. A separately pinned `CLAUDE_CODE_AUTO_MODE_MODEL` disables this Nyanpasu recovery path. Each new task starts from its own resolved primary target.

## Per-field resolution

At admission, the service resolves the backend, model, reasoning, and turn timeout, then persists the result on the task. Resolution uses:

1. A request's `execution_override`.
2. `tasks.kinds.<kind>.execution`.
3. `tasks.defaults.execution`.
4. The selected backend's `defaults` for model and reasoning.
5. An unspecified value, leaving the choice to the native CLI.

Model and reasoning resolve independently. Each task-policy layer belongs to the backend selected at that layer. A backend switch excludes model and reasoning values belonging to other backend names, even when both names use the same driver. If a request switches back to an earlier backend, its applicable earlier preferences are available again. Changing only the model within one backend retains the applicable reasoning setting.

For example, with global `backend = "codex"`, global `model = "general-model"`, and review-kind `backend = "claude"`, the review uses Claude's model default rather than `general-model`. A request that selects `codex` again can use `general-model`. To override only reasoning, supply `{"reasoning":"high"}`.

`tasks.kinds.<kind>.limits.turn_timeout_seconds` overrides `tasks.defaults.limits.turn_timeout_seconds`. Execution overrides accept only backend, model, and reasoning; they do not modify timeouts. The [complete example](../examples/config.toml) shows separate routing for review, independent design, PR creation, and memory consolidation.

Task kinds used by bundled plugins include:

| Kind                                 | Work                                                              |
| ------------------------------------ | ----------------------------------------------------------------- |
| `default`                            | A generic task without an explicit kind                           |
| `subtask`                            | A generic child task                                              |
| `github_reviewer.review`             | A PR review                                                       |
| `github_reviewer.independent-design` | A reviewer design child with memory disabled                      |
| `github_reviewer.<purpose>`          | Other reviewer children when the caller leaves `kind = "subtask"` |
| `github_pr_maker.create`             | PR creation                                                       |
| `github_pr_maker.followup`           | Work on an existing managed PR                                    |
| `memory_consolidation`               | Evidence-based memory maintenance after a completed root task     |

A child resolves its own kind and explicit execution override. It does not implicitly inherit the parent's execution target. The control request uses `execution`; an `AgentTask` JSON file uses `execution_override`. Both accept a structured object or compact `backend[/model][:reasoning]`, such as `codex`, `claude:high`, or `codex/your-model:high`. Use an object for model IDs containing `/` or `:`.

```bash
uv run nyanpasu explain-target --kind github_reviewer.review
uv run nyanpasu explain-target --kind github_reviewer.review --backend codex --reasoning high
uv run nyanpasu run-task task.json --target codex/your-model:high
```

The CLI also accepts separate `--backend`, `--model`, and `--reasoning` overrides for `run-task`. `explain-target` and the Runtime page show where each resolved field came from. An unspecified model is reported as native selection, not as a verified model name. A configured primary model and a model actually used after native fallback are different facts.

Restart to reload configuration. Changed defaults affect newly admitted tasks. Queued and interrupted tasks retain their persisted backend, model, reasoning, and timeout. Recovery resumes the saved native session and workspace; it does not silently move admitted work to a new default backend. A new task that changes the context's backend starts a fresh native session while preserving the context workspace and historical session references. Changing the memory audience also prevents session reuse.

Queued tasks coalesce only when their execution intent and memory access are compatible, as well as satisfying the plugin and context rules. Different resolved targets do not share a merged execution.

## Environment overrides

Use the uniform `NYANPASU__<SECTION>__<FIELD>` namespace. Values are TOML literals, with unquoted non-TOML text accepted as strings. Arrays and objects therefore use TOML syntax; quote a string that otherwise looks like a number or boolean.

```bash
export NYANPASU__SERVER__TOKEN='a-random-bearer-token'
export NYANPASU__TASKS__DEFAULTS__EXECUTION__BACKEND=claude
export NYANPASU__BACKENDS__CLAUDE__DEFAULTS__MODEL=your-model
export NYANPASU__TASKS__DEFAULTS__LIMITS__TURN_TIMEOUT_SECONDS=7200
export NYANPASU__PLUGINS__ENABLED='["github_reviewer"]'
```

Structural names are case-insensitive in the override path. A child-process environment key preserves its spelling: `NYANPASU__BACKENDS__CODEX__PROCESS__ENV__GH_PROMPT_DISABLED='"1"'` sets `GH_PROMPT_DISABLED`, not a lowercase variable. `NYANPASU_HOME` remains the dedicated home-directory setting. Old configuration environment variables are rejected, including `NYANPASU_TOKEN`; this prevents an obsolete authentication override from being silently ignored.

## Linux execution isolation

All execution backends require Linux and [bubblewrap](../DEPENDENCIES/bubblewrap.md), including Claude Code. Each context/backend/memory-audience combination gets its own native home. Native config and authentication are seeded separately from personal memories and conversation history. Nyanpasu disables Codex native memory and Claude auto-memory only for its worker processes; it does not modify the operator's normal CLI settings.

`backends.<name>.home.native_directory` locates native CLI state relative to the isolated `HOME`; defaults are `.codex` and `.claude`. It must stay within that home. Wrappers that hardcode another location need an explicit relative path, for example `.cc-mirror/codewiz-cc/config`.

`backends.<name>.home.template` optionally points to an administrator-curated home tree containing only the configuration, authentication, and skills needed by that backend. Its layout is relative to `HOME`, including the selected native directory and any wrapper-specific files. Templates must contain real files and directories; symbolic links are rejected. A template supplies the complete seed and is not supplemented from the operator’s home. Never point it at a full personal home or copy personal memories, conversation histories, or the service's administrative credentials into it. Without a template, Nyanpasu seeds only the driver's supported configuration and authentication files. Existing isolated files are retained, including refreshed credentials; template changes require a fresh context or an explicit maintenance procedure.

```toml
[backends.claude.home]
native_directory = ".claude"
template = "/opt/nyanpasu-home-templates/claude"
```

`isolation.readonly_paths` explicitly grants required executable, library, tool, and skill locations beyond the standard system directories. Grant the smallest relevant directories. Do not expose an entire user home, Nyanpasu state directory, another context's native home, or memory storage. A path grant makes files visible; the native CLI must also be configured to discover the intended skills and integrations.

```toml
[isolation]
readonly_paths = ["/opt/nyanpasu-tools", "/opt/nyanpasu-skills"]
```

The workspace and isolated native home are writable; the task control capability is mounted for that turn. Networking remains available, so keep the service's administrative bearer token out of backend credentials and native configuration. Any non-public memory domain requires `server.token`; the service also refuses to expose existing non-public task history without authentication. A deployment must permit bubblewrap's namespaces and mounts; verify a real shell tool invocation under each configured driver before serving work.

## Migrating a previous installation

Old `[codex]`, `[claude]`, `runtime.backend`, `enabled_plugins`, and direct `plugins.<id>` settings are rejected. Runtime loading does not translate old configuration or state schemas.

1. Drain active work, stop the service, and retain backups of the configuration and SQLite state.
2. From the updated source checkout, generate a new configuration file:

   ```bash
   uv run python scripts/migrate-config.py "$NYANPASU_HOME/config.toml" "$NYANPASU_HOME/config.next.toml"
   ```

   The script never overwrites its destination and creates it with mode `0600`. It preserves the active backend's turn timeout. If an inactive backend had a different timeout, it reports that value so you can assign it to the appropriate task kinds. Inspect the generated file, prepare minimal native home templates where needed, add the required isolation paths, and replace the active configuration through your deployment procedure.

3. Update the service environment. Common replacements are:

   | Old setting                        | New setting                                                                 |
   | ---------------------------------- | --------------------------------------------------------------------------- |
   | `NYANPASU_TOKEN`                   | `NYANPASU__SERVER__TOKEN`                                                   |
   | `NYANPASU_HOST` / `NYANPASU_PORT`  | `NYANPASU__SERVER__HOST` / `NYANPASU__SERVER__PORT`                         |
   | `NYANPASU_BACKEND`                 | `NYANPASU__TASKS__DEFAULTS__EXECUTION__BACKEND`                             |
   | `NYANPASU_CODEX_MODEL`             | `NYANPASU__BACKENDS__CODEX__DEFAULTS__MODEL`                                |
   | `NYANPASU_CLAUDE_REASONING_EFFORT` | `NYANPASU__BACKENDS__CLAUDE__DEFAULTS__REASONING`                           |
   | `NYANPASU_COMMAND_TIMEOUT_SECONDS` | `NYANPASU__TASKS__DEFAULTS__LIMITS__TURN_TIMEOUT_SECONDS`                   |
   | `NYANPASU_PLUGINS`                 | `NYANPASU__PLUGINS__ENABLED`, using a TOML array                            |
   | `NYANPASU_CODEX_BIN`               | `NYANPASU__BACKENDS__CODEX__PROCESS__COMMAND`, using the full command array |

4. With the new configuration and environment installed, use `explain-target` to inspect routing. Migrate the stopped, backed-up state with explicit historical directories:

   ```bash
   uv run nyanpasu migrate-state "$NYANPASU_HOME/state.sqlite3" \
     --native-home "codex=$HOME/.codex" \
     --native-home "claude=$HOME/.claude"
   ```

   Supply each historical backend's actual history directory. A wrapper's configuration directory may link to history elsewhere; use the directory containing the real `projects` or `sessions` tree. Reader access is confined to that native directory by default; `--isolated-home backend=/absolute/path` explicitly changes its boundary when required. Migration clears active context thread bindings, so future work starts fresh isolated native sessions. Old conversations remain available through their historical references while their history files are retained.
5. Restart, verify authentication and `/health`, inspect Runtime and the task queue, and run a bounded task through each configured driver.

The configuration converter does not alter native CLI settings or import personal native memories into the shared corpus. Review task evidence through the normal [memory workflow](memory.md) before storing reusable knowledge.
