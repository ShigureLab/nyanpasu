# Nyanpasu

Nyanpasu is a plugin-oriented Codex agent service. The core runtime is deliberately generic: it accepts events from plugins, turns them into `AgentTask` objects, prepares one reusable workspace per context, reuses persistent Codex threads per context, records state in SQLite, and runs Codex under a constrained runtime policy.

GitHub PR review is implemented by the `nyanpasu-github-reviewer` plugin, not by the core package. Shared GitHub helpers live in `nyanpasu-github` so GitHub-facing plugins can reuse repo config, workspace refs, webhook signatures, and agent task helpers without coupling those features to the core runtime.

## Core Responsibilities

- Async task execution with per-context serialization and bounded concurrency.
- Context workspace management. By default, one `context_key` owns one reusable managed clone that is reset to the task revision before each run.
- Optional event snapshots for plugins that explicitly need per-event isolation.
- Persistent task, thread, and context state.
- Codex backend management through `codex app-server` or `codex exec`.
- Plugin lifecycle hooks, HTTP router registration, and post-process hooks.
- Runtime safety defaults: `sandbox = "workspace-write"`, `approval_policy = "on-request"`, and `approvals_reviewer = "auto_review"`.

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
sandbox = "workspace-write"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
command_timeout_seconds = 3600
pass_env = ["NYANPASU_GITHUB_TOKEN"]

[runtime]
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

Instruction documents are task-scoped. A plugin can attach files such as `SOUL.md`, `AGENTS.md`, or project policy notes to an `AgentTask`; the core runtime appends them only for that task before invoking Codex. They are not global Nyanpasu identity and are not hardcoded into the core or GitHub reviewer prompt.

Integration config is generic core data. Nyanpasu core stores `integrations` as plain TOML tables; packages such as `nyanpasu-github` parse their own integration settings. For GitHub, `token_env` is preferred over writing a token directly in TOML. If neither `token` nor `token_env` is set, GitHub plugins fall back to the ambient `gh auth` state.

Agent-driven GitHub tasks that run `gh` inside Codex, such as PR maker, also need the token environment variable to be visible to the Codex runtime. Add that variable name to `codex.pass_env`, for example `pass_env = ["NYANPASU_GITHUB_TOKEN"]`. Nyanpasu records the variable name in prompts and task plans, not the token value.

To define environment variables specifically for Codex, use `codex.env`. A string is a literal value; a `cmd` table reads a value from a command's stdout:

```toml
[codex.env]
TZ = "Asia/Shanghai"
GH_PROMPT_DISABLED = "1"
GH_TOKEN = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }
```

For a static value, use `GH_TOKEN = "your-token"` instead. Values in `codex.env` override the base environment and `codex.pass_env`; their names do not need to appear in `pass_env`.

Commands run once when the service is created, with the original service environment and `NYANPASU_HOME` as the working directory. Arguments are passed directly without a shell, and commands cannot reference other `codex.env` entries. Only trailing CR/LF characters are removed from stdout. A command that cannot start, exits unsuccessfully, exceeds 10 seconds, or returns empty, invalid UTF-8, or NUL-containing output prevents startup. Errors identify the variable and failure without including command output. Resolved values stay in memory and are reused for every Codex turn, including app-server restarts; restart Nyanpasu to refresh them.

`codex.env` only affects Codex child processes. It does not modify the service environment or configure plugin-side GitHub credentials: `integrations.github.token_env` still reads the service's environment. Pinning `GH_TOKEN` this way prevents local `gh auth switch` from changing the account used by Codex.

`approval_policy` and `approvals_reviewer` are separate Codex controls. `approval_policy` decides when an approval request is created; `approvals_reviewer = "auto_review"` routes those requests to Codex's automatic approval reviewer instead of a human prompt. Set `approval_policy = "never"` only when you want failed or blocked operations returned directly to the model with no approval path.

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

Open the dashboard to read session transcripts, inspect tool input/output and failures, search saved content, and follow related tasks:

```text
http://127.0.0.1:8765/dashboard
```

Legacy results remain readable with explicit capture gaps; new executions record input and backend events while they run.

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

The GitHub PR maker plugin mounts:

```text
POST /plugins/github-pr-maker/tasks
GET /plugins/github-pr-maker/tasks/{task_id}
```

It accepts a repository and task description, builds a concrete PR plan, and asks Codex to implement the change, create a branch, commit, push, and open one pull request with `gh pr create` inside the managed worktree. The post-process hook only parses the agent's final `PR: <url>` or `NO_PR: <reason>` marker and records the result. Core still never performs GitHub writes directly.

When `follow_up_enabled = true`, PR maker records PRs it created and polls only those PRs for actionable follow-up signals. A follow-up task reuses the original task context key and PR branch workspace, so Codex keeps the same thread and the core runtime serializes work for that PR. Follow-up tasks ask Codex to commit and push to the existing PR branch instead of opening a second PR.

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

By default, the task uses `workspace_policy = "context"`: Nyanpasu resets the context workspace to `workspace.revision` or `workspace.ref`, runs Codex there, and keeps that workspace for the next event in the same context. Plugins can opt into `workspace_policy = "event_snapshot"` only when they need a disposable per-event workspace.
