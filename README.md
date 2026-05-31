# Nyanpasu

Nyanpasu is a plugin-oriented Codex agent service. The core runtime is deliberately generic: it accepts events from plugins, turns them into `AgentTask` objects, prepares one reusable workspace per context, reuses persistent Codex threads per context, records state in SQLite, and runs Codex under a constrained runtime policy.

GitHub PR review is implemented by the `nyanpasu-github-reviewer` plugin, not by the core package.

## Core Responsibilities

- Async task execution with per-context serialization and bounded concurrency.
- Context workspace management. By default, one `context_key` owns one reusable worktree that is reset to the task revision before each run.
- Optional event snapshots for plugins that explicitly need per-event isolation.
- Persistent task, thread, and context state.
- Codex backend management through `codex app-server` or `codex exec`.
- Plugin lifecycle hooks, HTTP router registration, and post-process hooks.
- Runtime safety defaults: `sandbox = "workspace-write"`, `approval_policy = "on-request"`, and `approvals_reviewer = "auto_review"`.

Anything domain-specific belongs in a plugin. GitHub event parsing, polling, webhook signatures, `gh-llm` prompts, and review submission live in `packages/nyanpasu-github-reviewer`.

## Configuration

Nyanpasu uses TOML and Pydantic models. Core config lives at the top level; plugin config lives under `plugins.<plugin_id>`.

Nyanpasu has one user-facing home directory. Set `NYANPASU_HOME` to choose it; otherwise it defaults to `~/.nyanpasu`. Config is always read from `$NYANPASU_HOME/config.toml`, and runtime state, logs, SQLite, and managed worktrees also live under `$NYANPASU_HOME`.

`state_dir` is intentionally not a TOML option. To move both config and state, move `NYANPASU_HOME`.

```toml
enabled_plugins = ["github_reviewer"]

[server]
host = "127.0.0.1"
port = 8765

[codex]
backend = "app-server"
sandbox = "workspace-write"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
command_timeout_seconds = 3600

[runtime]
concurrency = 4
coalesce_window_seconds = 600
clean_event_snapshots = true

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
```

Instruction documents are task-scoped. A plugin can attach files such as `SOUL.md`, `AGENTS.md`, or project policy notes to an `AgentTask`; the core runtime appends them only for that task before invoking Codex. They are not global Nyanpasu identity and are not hardcoded into the core or GitHub reviewer prompt.

`approval_policy` and `approvals_reviewer` are separate Codex controls. `approval_policy` decides when an approval request is created; `approvals_reviewer = "auto_review"` routes those requests to Codex's automatic approval reviewer instead of a human prompt. Set `approval_policy = "never"` only when you want failed or blocked operations returned directly to the model with no approval path.

## Run

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

The GitHub reviewer plugin mounts its webhook at:

```text
POST /plugins/github-reviewer/webhook
```

The plugin can also start its poller during plugin setup. GitHub reviewer polling combines repository events, PR state polling, and PR timeline polling into one event journal: the first poll records current cursors and PR snapshots without handling older work, later polls process matching events after those cursors, and already processed delivery ids are skipped. `poll_max_events_per_cycle = 0` means dispatch every matching journal event in the poll window; set it to a positive number only when you intentionally want a per-cycle cap.

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

    async def shutdown(self):
        ...
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

By default, the task uses `workspace_policy = "context"`: Nyanpasu resets the context worktree to `workspace.revision` or `workspace.ref`, runs Codex there, and keeps that workspace for the next event in the same context. Plugins can opt into `workspace_policy = "event_snapshot"` only when they need a disposable per-event worktree.
