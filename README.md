# Nyanpasu

Nyanpasu is a plugin-oriented agent service. The core runtime is deliberately generic: it accepts events from plugins, turns them into `AgentTask` objects, prepares one reusable workspace per context, reuses native agent sessions per context, records state in SQLite, and runs the selected agent with explicit runtime settings.

GitHub PR review is implemented by the `nyanpasu-github-reviewer` plugin, not by the core package. Shared GitHub helpers live in `nyanpasu-github` so GitHub-facing plugins can reuse repo config, workspace refs, webhook signatures, and agent task helpers without coupling those features to the core runtime.

## Core Responsibilities

- Async task execution with per-context serialization and bounded concurrency.
- Context workspace management. By default, one `context_key` owns one reusable managed clone that is reset to the task revision before each run.
- Optional event snapshots for plugins that explicitly need per-event isolation.
- Persistent task, thread, and context state.
- Pluggable execution backends: Codex and Claude Code.
- Plugin lifecycle hooks, HTTP router registration, and post-process hooks.
- Runtime safety defaults: automatic permission review for both Codex (`approvals_reviewer = "auto_review"`) and Claude Code (`permission_mode = "auto"`). Codex also defaults to `sandbox = "workspace-write"` and `approval_policy = "on-request"`.

Anything domain-specific belongs in a plugin. GitHub event parsing, polling, `gh-llm` prompts, review submission, and PR creation live under `packages/`. Reusable GitHub primitives live in `packages/nyanpasu-github`; the core package remains GitHub-agnostic.

## Configuration

Nyanpasu uses TOML and Pydantic models. Core config lives at the top level; plugin config lives under `plugins.<plugin_id>`.

Nyanpasu has one user-facing home directory. Set `NYANPASU_HOME` to choose it; otherwise it defaults to `~/.nyanpasu`. Config is always read from `$NYANPASU_HOME/config.toml`, and runtime state, logs, SQLite, and managed workspaces also live under `$NYANPASU_HOME`.

`state_dir` is intentionally not a TOML option. To move both config and state, move `NYANPASU_HOME`.

```toml
enabled_plugins = ["github_reviewer", "github_pr_maker"]

[server]
host = "127.0.0.1"
port = 8765

[codex]
backend = "app-server"
model = "gpt-6-astra"
reasoning_effort = "medium"
sandbox = "workspace-write"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
command_timeout_seconds = 3600
pass_env = ["NYANPASU_GITHUB_TOKEN"]

[claude]
model = "sonnet"
reasoning_effort = "medium"
permission_mode = "auto"
pass_env = ["ANTHROPIC_API_KEY", "NYANPASU_GITHUB_TOKEN"]

[runtime]
backend = "codex" # or "claude"
concurrency = 4
coalesce_window_seconds = 600
clean_event_snapshots = true

[integrations.github]
token_env = "NYANPASU_GITHUB_TOKEN"
git_author_name = "Nyanpasu"
git_author_email = "nyanpasu@example.invalid"

[plugins.github_reviewer]
github_login = "your-github-login"
poll_interval_seconds = 600
poll_event_pages = 3
poll_max_events_per_cycle = 0
review_language = "Chinese"

[[plugins.github_reviewer.instruction_docs]]
name = "SOUL.md"
path = "/path/to/SOUL.md"

[plugins.github_reviewer.repos."owner/repo"]
local_path = "/path/to/repo"
github_remote = "https://github.com/owner/repo.git"
base_branches = ["main"]

[[plugins.github_reviewer.repos."owner/repo".instruction_docs]]
name = "AGENTS.md"
path = "/path/to/repo/AGENTS.md"
required = false

[plugins.github_pr_maker]
branch_prefix = "nyanpasu"
default_base_branch = "main"
dry_run = false
draft = false
follow_up_enabled = true
follow_up_interval_seconds = 600

[plugins.github_pr_maker.repos."owner/repo"]
local_path = "/path/to/repo"
github_remote = "https://github.com/owner/repo.git"
base_branches = ["main"]
```

Instruction documents are task-scoped. A plugin can attach files such as `SOUL.md`, `AGENTS.md`, or project policy notes to an `AgentTask`; the core runtime appends them only for that task before invoking the selected backend. They are not global Nyanpasu identity and are not hardcoded into the core or GitHub reviewer prompt.

Integration config is generic core data. Nyanpasu core stores `integrations` as plain TOML tables; packages such as `nyanpasu-github` parse their own integration settings. GitHub plugins accept a literal `token`, a command-backed `token`, or `token_env`. Configure only one source. Credentials are resolved when each plugin starts and reused for its lifetime. A configured environment variable that is missing or empty prevents that backend from starting. If neither `token` nor `token_env` is set, GitHub plugins use the ambient `gh auth` state.

To pin plugin-side API calls to one locally authenticated account without storing a token in TOML:

```toml
[integrations.github]
token = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }
```

Token commands follow the same execution, validation, and error reporting rules as the runtime `env` commands below. They run once per plugin startup. Restart the service to refresh resolved credentials.

Agent-driven GitHub tasks that run `gh`, such as PR maker, also need the token environment variable to be visible to the selected runtime. Add that variable name to `codex.pass_env` or `claude.pass_env`, for example `pass_env = ["NYANPASU_GITHUB_TOKEN"]`. Nyanpasu records the variable name in prompts and task plans, not the token value.

## Runtime configuration

Choose `codex` or `claude` with `runtime.backend` or `NYANPASU_BACKEND`. Install and authenticate the selected CLI before starting Nyanpasu. Configure it in the corresponding `[codex]` or `[claude]` section; see the [complete example](examples/config.toml).

Both backends support `bin`, `args`, `model`, `reasoning_effort`, `command_timeout_seconds`, `env`, and `pass_env`. `bin` is an executable name or path, and `args` is a literal argument list that replaces the defaults when specified. Arguments are passed directly without a shell.

Set `model` and `reasoning_effort` to pin the agent's configuration for new and resumed sessions. Omitted values inherit the CLI's defaults. The Dashboard's Runtime page and the reviewer's disclosure footer use the configured values. `NYANPASU_CODEX_MODEL` / `NYANPASU_CLAUDE_MODEL` and `NYANPASU_CODEX_REASONING_EFFORT` / `NYANPASU_CLAUDE_REASONING_EFFORT` override the TOML settings.

Automatic permission review is enabled by default for both backends, including when these settings are omitted:

| Backend     | Default safety settings                                                                               |
| ----------- | ----------------------------------------------------------------------------------------------------- |
| Codex       | `sandbox = "workspace-write"`, `approval_policy = "on-request"`, `approvals_reviewer = "auto_review"` |
| Claude Code | `permission_mode = "auto"`                                                                            |

Codex routes approval requests to its automatic reviewer. Claude's [auto mode](https://code.claude.com/docs/en/permission-modes#eliminate-permission-prompts-with-auto-mode) uses a separate classifier for actions requiring approval and requires account and model support. Automatic checks do not guarantee safety. Claude's [Bash sandbox](https://code.claude.com/docs/en/sandboxing) is configured separately through its native settings; permission mode alone does not enable filesystem or network isolation.

Use `env` for literal or command-backed environment variables and `pass_env` to forward named variables from the service environment. For example:

```toml
[codex.env]
TZ = "Asia/Shanghai"
GH_PROMPT_DISABLED = "1"
GH_TOKEN = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }
```

The same settings work under `[claude.env]`. Values in `env` override inherited values and `pass_env`; their names do not need to appear in `pass_env`. These settings affect agent child processes, while `integrations.github` configures plugin-side GitHub credentials. Configure both for the same account when the agent and plugin need to publish as one bot.

Environment commands run once when the backend is first used, with the service environment and `NYANPASU_HOME` as the working directory. They cannot reference other `env` entries. Trailing CR/LF characters are removed from stdout. Failure, a timeout after 10 seconds, or empty, invalid UTF-8 or NUL-containing output prevents startup. Errors omit command output, and resolved values stay in memory until the service restarts.

Restart Nyanpasu after configuration changes. Changing `runtime.backend` makes the next task in an existing context start a new native session, retaining its context key and workspace. Tasks resume the current session while the backend stays the same; switching back also creates a new session. Previous conversations remain available in history and are not migrated. Keep the original runtime and history files available to read them.

## Run

For direct Uvicorn use, load the application factory with `uvicorn nyanpasu.web:app_from_env --factory`.

Create `$NYANPASU_HOME/config.toml` from `examples/config.toml`, then start the agent service:

```bash
export NYANPASU_HOME="$HOME/.nyanpasu"
uv run nyanpasu serve
```

Inspect runtime state:

```bash
uv run nyanpasu status
curl http://127.0.0.1:8765/tasks
curl http://127.0.0.1:8765/contexts
```

Open the dashboard to read session transcripts, inspect tool input/output and failures, search native conversation history, and follow related tasks:

```text
http://127.0.0.1:8765/dashboard
```

Each backend owns its native conversation history. Nyanpasu stores scheduling metadata and session references; the Dashboard reads native messages, reasoning, tool calls, edits, and results without maintaining another conversation database. Session details identify the backend and native session ID. Historical conversations remain readable after changing backends while their runtime and history files remain available.

The dashboard frontend is built with Vite+ and managed with pnpm. Use the pnpm
version pinned in `package.json`. During development, use:

```bash
pnpm install --frozen-lockfile
pnpm run dev
```

With `uv run nyanpasu serve` running in another terminal, open
`http://localhost:5173/dashboard/assets/`. The development server reloads frontend
changes and proxies `/api` requests to the backend at `127.0.0.1:8765`.

Check, test, and build the dashboard with:

```bash
pnpm run types
pnpm run check
pnpm run test
pnpm run build
```

After `uv sync --dev`, `pnpm run types` regenerates the transcript TypeScript contract from the Python models. To run browser interaction tests with an isolated fixture server:

```bash
pnpm exec playwright install --with-deps chromium
pnpm run test:browser
```

Generated files in `src/nyanpasu/dashboard_static/` are ignored by Git. For the
backend-served `/dashboard` page, run `pnpm run build` before starting the service.
The development server serves frontend source directly and does not need these
generated files. To build Python distributions with the Dashboard included, run
`just build`, or run `pnpm run build` followed by `uv build`.

The GitHub reviewer plugin mounts its webhook at:

```text
POST /plugins/github-reviewer/webhook
```

The plugin can also start its poller during plugin setup. GitHub reviewer polling combines repository events, PR state polling, and PR timeline polling into one event journal: the first poll records current cursors and PR snapshots without handling older work, later polls process matching events after those cursors, and already processed delivery ids are skipped. `poll_max_events_per_cycle = 0` means dispatch every matching journal event in the poll window; set it to a positive number only when you intentionally want a per-cycle cap.

The reviewer prompt directs the agent to use the `gh-slate` skill and CLI to maintain a named PR dashboard comment with review status, conclusions, and links to finding threads. See the [review dashboard workflow](packages/nyanpasu-github-reviewer/README.md#review-dashboard) for setup and update rules.

The GitHub PR maker plugin mounts:

```text
POST /plugins/github-pr-maker/tasks
GET /plugins/github-pr-maker/tasks/{task_id}
```

It accepts a repository and task description, builds a concrete PR plan, and asks the selected agent to implement the change, create a branch, commit, push, and open one pull request with `gh pr create` inside the managed worktree. The post-process hook only parses the agent's final `PR: <url>` or `NO_PR: <reason>` marker and records the result. Core still never performs GitHub writes directly.

When `follow_up_enabled = true`, PR maker records PRs it created and polls only those PRs for actionable follow-up signals. A follow-up task reuses the original task context key and PR branch workspace, so the agent keeps the same session while the backend stays the same, and the core runtime serializes work for that PR. Follow-up tasks ask the agent to commit and push to the existing PR branch instead of opening a second PR.

## Plugin Contract

A plugin exposes a `NyanpasuPlugin` through the `nyanpasu.plugins` entry point group.

```python
class MyPlugin:
    id = "my_plugin"
    config_model = MyPluginConfig

    async def setup(self, runtime, config):
        runtime.add_router(router, prefix="/plugins/my-plugin")
        runtime.add_post_process_hook(self.id, self.after_task)
        await runtime.submit(task)

    async def shutdown(self): ...
```

Plugins send work to the core by creating `AgentTask`:

```python
AgentTask(
    task_id="event-123",
    action=TaskAction.RUN,
    context_key="my-domain:object-456",
    prompt="Review or handle this event.",
    developer_instructions="Continue this object's task across turns and follow the configured publication policy.",
    instruction_docs=[
        InstructionDocument(
            name="AGENTS.md",
            source="/path/to/repo/AGENTS.md",
            content="Follow this repository's local conventions.",
        ),
    ],
    workspace=WorkspaceRef(
        key="owner/repo",
        local_path=Path("/path/to/repo"),
        remote="https://github.com/owner/repo.git",
        ref="pull/123/head",
        revision="abc123",
    ),
    dedupe_key="event-123",
    metadata={"plugin_id": "my_plugin"},
)
```

Core executes the task and calls post-process hooks registered for `metadata["plugin_id"]`.

`developer_instructions` and configured `instruction_docs` form the session instructions, applied when creating or resuming a session alongside the agent's built-in instructions. `prompt` is the current turn's user message. Keep changing facts and requests there, and use skills or reference documents for detailed tool workflows. The Dashboard renders the native turn input without prepending session instructions.

Plugins that need current external state before execution can register `runtime.add_task_preparer(self.id, self.prepare_task)`. The async preparer receives `(task, coalesced_tasks, context)` after the context lease is acquired, and returns a task with the current workspace, instructions, and message. Keep the task ID and context key unchanged; return an ignored task when the work is no longer applicable.

Task merging is opt-in with a `coalesce_key` and requires a registered preparer. Compatible queued tasks with the same key, plugin, and context can merge within `runtime.coalesce_window_seconds`; the preparer owns their domain-specific merge. Ordinary tasks remain separate. Recording and merging happen in one transaction, and a running task cannot receive late merged events. This window does not delay task execution or guarantee that nearby events will share a turn.

By default, the task uses `workspace_policy = "context"`: Nyanpasu resets the context workspace to `workspace.revision` or `workspace.ref`, runs the selected backend there, and keeps that workspace for the next event in the same context. Plugins can opt into `workspace_policy = "event_snapshot"` only when they need a disposable per-event workspace.
