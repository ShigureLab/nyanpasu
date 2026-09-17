# Nyanpasu

Nyanpasu is a plugin-oriented agent service supporting Codex and Claude Code. The core runtime is deliberately generic: it accepts events from plugins, turns them into `AgentTask` objects, prepares one reusable workspace per context, reuses native agent sessions per context, records state in SQLite, and runs the selected agent with explicit runtime settings.

GitHub PR review is implemented by the `nyanpasu-github-reviewer` plugin, not by the core package. Shared GitHub helpers live in `nyanpasu-github` so GitHub-facing plugins can reuse repo config, workspace refs, webhook signatures, and agent task helpers without coupling those features to the core runtime.

## Core Responsibilities

- Async task execution with per-context serialization and bounded concurrency.
- Context workspace management. By default, one `context_key` owns one reusable managed clone that is reset to the task revision before each run.
- Optional event snapshots for plugins that explicitly need per-event isolation.
- Persistent task, thread, and context state.
- Codex (`app-server` or `exec`) and Claude Code (`claude -p`) backends, including compatible wrapper executables.
- Plugin lifecycle hooks, HTTP router registration, and post-process hooks.
- Codex defaults: `sandbox = "workspace-write"`, `approval_policy = "on-request"`, and `approvals_reviewer = "auto_review"`.
- Claude permissions: examples use `permission_mode = "auto"` for automatic permission checks; omitting the setting defaults to `dontAsk`. Use `allowed_tools` to pre-approve tools.

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

Token commands follow the same execution, validation, and error reporting rules as `codex.env` commands below. They run once per plugin startup. Restart the service to refresh resolved credentials.

Agent-driven GitHub tasks that run `gh`, such as PR maker, also need the token environment variable to be visible to the selected runtime. Add that variable name to `codex.pass_env` or `claude.pass_env`, for example `pass_env = ["NYANPASU_GITHUB_TOKEN"]`. Nyanpasu records the variable name in prompts and task plans, not the token value.

Set `codex.model` and `codex.reasoning_effort` to pin Nyanpasu's model independently of your interactive Codex configuration. Both `app-server` and `exec` apply these settings to every turn, including resumed sessions. `NYANPASU_CODEX_MODEL` and `NYANPASU_CODEX_REASONING_EFFORT` override the TOML values. An omitted setting inherits Codex's defaults; use a reasoning effort supported by the selected model. Restart Nyanpasu after changing the configuration. The Dashboard's Runtime page shows the configured values, and the reviewer's disclosure footer uses the same configuration.

To define environment variables specifically for Codex, use `codex.env`. A string is a literal value; a `cmd` table reads a value from a command's stdout:

```toml
[codex.env]
TZ = "Asia/Shanghai"
GH_PROMPT_DISABLED = "1"
GH_TOKEN = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }
```

For a static value, use `GH_TOKEN = "your-token"` instead. Values in `codex.env` override the base environment and `codex.pass_env`; their names do not need to appear in `pass_env`.

Commands run once when that backend is first used, with the original service environment and `NYANPASU_HOME` as the working directory. Arguments are passed directly without a shell, and commands cannot reference other `codex.env` entries. Only trailing CR/LF characters are removed from stdout. A command that cannot start, exits unsuccessfully, exceeds 10 seconds, or returns empty, invalid UTF-8, or NUL-containing output prevents that backend from starting. Errors identify the variable and failure without including command output. Resolved values stay in memory and are reused for every Codex turn, including app-server restarts; restart Nyanpasu to refresh them.

`codex.env` only affects Codex child processes. It does not modify the service environment or configure plugin-side GitHub credentials: `integrations.github.token_env` reads the service's environment at plugin startup. Configure both `codex.env.GH_TOKEN` and `integrations.github.token` for the same account when the agent and plugin need to publish as one bot. Pinning credentials this way prevents local `gh auth switch` from changing the account used by a running service.

`approval_policy` and `approvals_reviewer` are separate Codex controls. `approval_policy` decides when an approval request is created; `approvals_reviewer = "auto_review"` routes those requests to Codex's automatic approval reviewer instead of a human prompt. Set `approval_policy = "never"` only when you want failed or blocked operations returned directly to the model with no approval path.

## Claude Code and wrapper executables

Choose the execution backend with `runtime.backend` or `NYANPASU_BACKEND`. After changing it and restarting Nyanpasu, the next task in an existing context starts a new native session on the selected backend, retaining its context key and workspace. Later tasks resume that session while the backend stays the same; switching back also starts a new session. Previous sessions remain available in history, but their conversations are not migrated. Keep both backends configured to read mixed history.

```toml
[runtime]
backend = "claude"

[claude]
bin = "/path/to/claude" # defaults to "claude" on PATH
model = "sonnet" # Claude Code alias; a full model ID also works
permission_mode = "auto"
allowed_tools = ["Read", "Grep", "Glob", "Bash(gh *)", "Bash(git diff *)"]
command_timeout_seconds = 3600
pass_env = ["ANTHROPIC_API_KEY", "NYANPASU_GITHUB_TOKEN"]

[claude.env]
CLAUDE_CONFIG_DIR = "/path/to/claude-home"
```

Install and authenticate [Claude Code](https://code.claude.com/docs/en/setup) before using it. This integration was verified with Claude Code **2.1.204** and **2.1.270** and requires stream-JSON input/output, user-message replay, session resume, and the native session format. Compatible wrappers must preserve these capabilities. `claude auth status` checks authentication.

`claude.args` defaults to `["--permission-prompts", "none", "--system-prompt-snapshot", "off"]`. For older CLIs that reject these options, including **2.1.204**, override the list:

```toml
[claude]
args = []
```

An explicit `args` list **replaces** the defaults. When adding wrapper or CLI arguments, include any default options you still need and the CLI supports. Nyanpasu does not infer option support from help text or version numbers.

`sonnet` is an official [Claude Code model alias](https://code.claude.com/docs/en/model-config#model-aliases). Its resolved version depends on the CLI, provider and local model settings. To pin a version, set a full model ID such as `claude-sonnet-5`, or the deployment ID required by your provider. Nyanpasu passes the configured value directly to `--model`.

Claude starts one process per task and resumes the persisted session on the next task. Its final `result` determines success; an error result fails the task even if the process exits with status zero. Timeout and shutdown terminate the process group. Cleanup releases the context/workspace and preserves Claude's native history. Claude's own retention settings determine how long old transcripts remain available.

`permission_mode` and `allowed_tools` are Claude settings, independent of Codex sandbox and approval policies. If `permission_mode` is omitted, the default `dontAsk` denies operations requiring ungranted permissions. Configure the tools the workflow actually needs; PR creation needs more write permissions than a read-only inspection. The default arguments include `--permission-prompts none` so unattended work does not wait for a terminal answer. Session instructions are appended to Claude's built-in prompt on every resumed turn. On CLIs supporting prompt snapshots, retain `--system-prompt-snapshot off` in `args` so a saved prompt does not override updated instructions.

The examples select `permission_mode = "auto"` for Claude's [automatic permission checks](https://code.claude.com/docs/en/permissions#permission-modes), which require account and model support. Its [Bash sandbox](https://code.claude.com/docs/en/sandboxing) is a separate filesystem/network boundary, configured through Claude's native settings (`sandbox.enabled`, `sandbox.allowUnsandboxedCommands`, and `sandbox.failIfUnavailable`). These settings can also be passed through `claude.args` with `--settings`. Nyanpasu leaves sandbox configuration to Claude; selecting a permission mode alone does not enable sandboxing.

Both `[claude]` and `[codex]` share `bin`, `args`, `model`, `reasoning_effort`, `command_timeout_seconds`, `env`, and `pass_env`. For example, a corporate wrapper can receive its own prefix arguments before Nyanpasu adds the agent protocol arguments:

```toml
[claude]
bin = "/opt/company/bin/claude-wrapper"
args = ["--profile", "review-bot"]

[codex]
bin = "/opt/company/bin/codex-wrapper"
args = ["--profile", "review-bot"]
backend = "app-server"
```

`bin` is a single executable name or path, not a shell command. Paths containing spaces work. Relative paths such as `./wrapper` are resolved against the service startup directory; executable symlinks are preserved. `args` is a literal argument list, with no shell interpolation. Codex uses this same command for execution and history access. Claude wrappers that change the history directory must expose the same `CLAUDE_CONFIG_DIR` through `claude.env` so both execution and the Dashboard find it.

`NYANPASU_CODEX_BIN` and `NYANPASU_CLAUDE_BIN` override executable paths. Claude also accepts `NYANPASU_CLAUDE_MODEL`, `NYANPASU_CLAUDE_REASONING_EFFORT`, and `NYANPASU_CLAUDE_PERMISSION_MODE`. `NYANPASU_COMMAND_TIMEOUT_SECONDS` overrides both backends' timeouts. Restart Nyanpasu after configuration changes.

Environment filtering and command-backed values work identically for both backends. API credentials and custom endpoint variables must be explicitly passed with `claude.pass_env` or set in `claude.env`; a local Claude login can be read through the inherited home directory. `CLAUDE_CONFIG_DIR` is inherited when present. Project instructions, skills and MCP settings are loaded by the selected CLI; install reviewer skills in Claude's supported skill locations when using that backend. Task `instruction_docs` can supply shared repository instructions explicitly.

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

Each backend owns its conversation history. Nyanpasu stores task scheduling metadata and backend/session/turn references. The Dashboard reads Codex history through app-server and Claude history from its native JSONL files, without maintaining another conversation database. Both sources produce the same message, reasoning, tool, file-change, search, export, and pagination contract. Tool inputs and results stay linked, and native timestamps are shown when available. Unknown content remains inspectable; unavailable metadata is not invented.

Existing database records migrate to the Codex backend. Codex session URLs stay unchanged; Claude URLs use `claude:<native-session-id>` so equal IDs from different backends cannot collide. Session details show the owning backend and the native ID to use with its CLI. History remains readable after changing the default backend, provided the original runtime and its history files remain available.

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

`developer_instructions` and configured `instruction_docs` form the session instructions. The Codex app-server backend binds them with `developerInstructions` on thread creation and resume; the exec backend uses the `developer_instructions` configuration override. Claude uses `--append-system-prompt`; its default `args` disable prompt snapshots for updated instructions on resume. Each agent’s built-in base instructions remain in place. `prompt` is the current turn's user message. Keep changing facts and requests there, and use skills or reference documents for detailed tool workflows. The Dashboard renders the native turn input without prepending session instructions.

Plugins that need current external state before execution can register `runtime.add_task_preparer(self.id, self.prepare_task)`. The async preparer receives `(task, coalesced_tasks, context)` after the context lease is acquired, and returns a task with the current workspace, instructions, and message. Keep the task ID and context key unchanged; return an ignored task when the work is no longer applicable.

Task merging is opt-in with a `coalesce_key` and requires a registered preparer. Compatible queued tasks with the same key, plugin, and context can merge within `runtime.coalesce_window_seconds`; the preparer owns their domain-specific merge. Ordinary tasks remain separate. Recording and merging happen in one transaction, and a running task cannot receive late merged events. This window does not delay task execution or guarantee that nearby events will share a turn.

By default, the task uses `workspace_policy = "context"`: Nyanpasu resets the context workspace to `workspace.revision` or `workspace.ref`, runs the selected backend there, and keeps that workspace for the next event in the same context. Plugins can opt into `workspace_policy = "event_snapshot"` only when they need a disposable per-event workspace.
