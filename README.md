<p align="center">
  <img src="frontend/dashboard/public/logo.png" alt="Nyanpasu logo" width="128" height="128" />
</p>

<h1 align="center">Nyanpasu</h1>

<p align="center">A plugin-oriented agent service.</p>

The core runtime is deliberately generic: it accepts events from plugins, turns them into `AgentTask` objects, prepares one reusable workspace per context, reuses native agent sessions per context, records state in SQLite, and runs the selected agent with explicit runtime settings.

GitHub PR review is implemented by the `nyanpasu-github-reviewer` plugin, not by the core package. Shared GitHub helpers live in `nyanpasu-github` so GitHub-facing plugins can reuse repo config, workspace refs, webhook signatures, and agent task helpers without coupling those features to the core runtime.

See the [dependency index](DEPENDENCIES/index.md) for required and optional tools, agent skills, prerequisites, and installation instructions.

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

Nyanpasu reads `$NYANPASU_HOME/config.toml`, with `NYANPASU_HOME` defaulting to `~/.nyanpasu`. State, logs, managed workspaces, separate native homes, and memory live below the same directory. `state_dir` is not a TOML option.

Backends have names and separate process settings, model defaults, and adapter options. Tasks select a backend independently, so Codex and Claude can run in the same service. Plugin activation lives under `plugins.enabled`, with plugin configuration under `plugins.settings.<id>`.

```toml
[backends.codex]
driver = "codex"

[backends.codex.defaults]
model = "your-codex-model"
reasoning = "high"

[backends.claude]
driver = "claude-code"

[backends.claude.process]
pass_env = ["ANTHROPIC_API_KEY", "NYANPASU_GITHUB_TOKEN"]

[backends.claude.defaults]
model = "your-review-model"
reasoning = "medium"

[tasks.defaults.execution]
backend = "codex"

[tasks.kinds."github_reviewer.review".execution]
backend = "claude"

[tasks.kinds.memory_extraction.execution]
backend = "codex"
model = "gpt-6-luna"
reasoning = "medium"

[tasks.kinds.memory_consolidation.execution]
backend = "codex"
model = "gpt-6-luna"
reasoning = "medium"

[plugins]
enabled = ["github_reviewer"]

[integrations.github]
token_env = "NYANPASU_GITHUB_TOKEN"

[plugins.settings.github_reviewer]
github_login = "your-github-login"

[plugins.settings.github_reviewer.repos."owner/repo"]
local_path = "/path/to/repo"
base_branches = ["main"]
```

The [complete example](examples/config.toml) lists backend options, native homes, task kinds, and memory settings. `process.command` is an argument array run without a shell; setting it replaces the driver's entire default command. `process.pass_env` forwards named service variables, while `process.env` overrides them and accepts either literal strings or commands such as `GH_TOKEN = { cmd = ["gh", "auth", "token"] }`.

Environment overrides use `NYANPASU__SECTION__FIELD`, for example `NYANPASU__TASKS__DEFAULTS__EXECUTION__BACKEND=claude`. Values use TOML syntax; arrays must be TOML arrays, and strings that look like numbers or booleans must be quoted. Child-process environment names keep their spelling, as in `NYANPASU__BACKENDS__CODEX__PROCESS__ENV__GH_PROMPT_DISABLED='"1"'`.

Instruction documents such as `SOUL.md` and `AGENTS.md` remain task-scoped. Plugins attach them to a task; the runtime supplies them to that task's native session. Integration credentials under `integrations.github` serve plugin-side API calls. Agent-side GitHub commands also need credentials in the selected backend's `process.env` or `process.pass_env`.

## Runtime configuration

At admission, the service resolves and records a task's backend, model, reasoning, and timeout. Each execution field uses the first applicable value from the request override, `tasks.kinds.<kind>.execution`, `tasks.defaults.execution`, and the selected backend's `defaults`. An unset model or reasoning leaves the choice to the native CLI. A backend switch excludes model/reasoning settings belonging to other backend names. Kind-specific `limits.turn_timeout_seconds` overrides the task default, which is 3600 seconds. A child resolves its own kind and overrides independently.

Queued and interrupted tasks retain their admitted target across configuration changes and resume their original native session and workspace. Newly admitted work uses the new settings. After an unclean exit, recovery waits for any remaining context lease to expire. Completed and failed tasks are not retried, and tasks from disabled plugins remain queued.

```bash
uv run nyanpasu explain-target --kind github_reviewer.review
uv run nyanpasu run-task task.json --target codex/your-model:high
```

The Runtime page shows configured backends, task-kind routing, and the source of each selected field. Configured intent is not proof of the model actually used after provider fallback. Claude's optional `backends.<name>.options.fallback_models` accepts up to three model names or `{ model = "your-backup-model", reasoning = "medium" }` entries; omitted fallback reasoning inherits the task's setting.

### Native session homes

Each context/backend/memory-audience combination has a separate native home for configuration and session history. `backends.<name>.home.native_directory` is relative to that home and defaults to `.codex` or `.claude`. An optional `home.template` supplies a minimal home tree containing configuration, authentication, and skills as real files; symbolic links are rejected. Without a template, the service seeds only the driver's supported configuration and authentication files. Existing context files are retained.

Both native automatic memory systems are disabled for Nyanpasu workers; normal interactive CLI settings remain unchanged. Keep personal memories, conversation histories, and service administrative credentials out of templates. Backend processes run directly with the service user's filesystem access and the native CLI's configured permissions.

## Background memory

Background memory scans completed root tasks with a contribution audience every 60 seconds and waits until their context has been idle for 5 minutes, with no queued, running, or waiting task. These intervals are configurable with `memory.sweep_interval_seconds` and `memory.idle_after_seconds`. Completed task records and extraction receipts are the durable backlog, so a restart retains work that has not yet been processed; an explicit `memory-rebuild` bypasses the idle window.

`memory_extraction` maintains one detailed Markdown account per audience, context, and context generation. It combines consecutive small contributions into bounded evidence batches, preserving each task's scope and chronology, and updates the existing account with useful new knowledge. Large contributions retain stable chunks for interrupted-work recovery. Extraction receipts advance only with a committed batch and prevent repeated processing. Task and native evidence references accumulate with the account, while original history remains available through tasks and transcripts.

The extracted account is capped at 16,000 characters. It prioritizes knowledge that can improve future decisions: substantive human feedback, corrected assumptions, concrete methods, design rationale, and verified failure modes, with their reasons, conditions, and sources. Routine requests, approvals, completion status, hashes, and dashboard bookkeeping do not merit retention on their own. Failed or unfinished work remains useful when future action depends on it; a session with nothing useful to retain may have an empty account. Human feedback retains its attribution and scope rather than becoming an unsupported technical fact or global preference.

`memory_consolidation` maintains one rolling summary per audience, context, and context generation, prioritizing these lessons within a **1 KiB UTF-8** limit for the complete injection block, including its heading, scope, and source references. Oversized output must be compressed before publication; a failed update never publishes an oversized or partial replacement. Summaries remain fallible accounts of evidence, so verify current repository, service, and PR state before acting on them.

Changing the account's title, body, or topics invalidates the previous short summary until consolidation refreshes it. Summary `source_revisions` fingerprint that content; source object revisions also include processing progress and provenance for concurrency checks. Advancing receipts without changing memory content preserves the summary and skips consolidation. A new empty account needs no summary job; withdrawing an earlier account clears its old summary. Existing current summaries migrate to content fingerprints without another model call. An unchanged follow-up should preserve useful earlier conclusions, conditions, and evidence links instead of reducing the account or summary to the latest task's status.

Each normal task turn automatically receives relevant, authorized context summaries. Selection prioritizes the current context, related contexts, then BM25-ranked task topic matches. Ranking accounts for term rarity, frequency saturation, and document length within the authorized, current, non-stale summaries. Complete blocks are packed into a **combined 8 KiB UTF-8 budget**, including the outer wrapper; stale summaries are excluded. This is a hard limit on the memory supplied for a turn, not a token count or a guarantee about the native client's entire accumulated context window.

Plugins may supply `metadata.memory_query` as a string for topic matching instead of the task prompt, and `metadata.memory_related_contexts` as a list of explicitly related context keys. Topic matching excludes generic words and terms from the current context key to avoid treating shared repository identifiers as relevant topics. These hints affect selection only and cannot widen `read_domains`.

The GitHub reviewer builds its memory query from the PR's `head_ref` and trigger `body_excerpt`, adding changed paths from `inventory.files[].path` for code reviews; CI-only reviews use the first two inputs, and neither mode includes the review prompt template in its topic query.

Tasks can follow summary evidence with `memory.describe`, `memory.search` (`query`, optional `topics` and `limit`), and `memory.read` (`source_id`). They have no memory write tools. Topics organize retrieval, while task capabilities determine access: `read_domains` selects readable audiences and `write_domain` selects the background contribution audience. Generic tasks default to no memory, and reviewer independent-design children disable it. GitHub tasks normally read `public` plus `shared:github:<owner/repo>` and contribute only to the latter. Repository memory settings can override these audiences; non-public audiences require `server.token`.

`memory.search` uses BM25 over context keys, titles, topics, and source bodies after applying audience and topic filters. Queries match any term prefix, including snake_case components and Chinese character pairs; use specific terms to improve ranking. An exact context key or historical task ID directly locates its account. Empty queries list authorized sources. Search results include an excerpt of at most 800 characters plus identity, title, topics, and revision; `memory.read` returns the full account and provenance. Retrieval is lexical, so it does not infer synonyms or translate queries. Both source search and summary selection compute rankings from the current Markdown records without a separate persistent index.

The Dashboard's global Memory view shows every stored audience for operator inspection, including private session accounts under **Sources** and their short summaries under **Summaries**. Each account shows its session, the task that last updated it, and the accumulated task and native evidence references. A task's **Inspect task memory** view shows only memory available to that task, plus recorded per-turn injections: the exact memory text, byte count, selected summary revisions, and reasons for selection or omission. An injection records what was supplied; it does not prove the agent used or cited it.

Both stages use the normal task queue and backend routing. Configure `tasks.kinds.memory_extraction` and `tasks.kinds.memory_consolidation` independently; an omitted kind inherits normal task defaults. The [complete example](examples/config.toml) gives them a separate backend with model credentials. `memory.enabled = false` disables all memory use; `memory.consolidate = false` disables production while retaining reads. `memory.max_results_per_search` sets the default and maximum result count from 1 to 100.

Retry failed maintenance for a completed source task with:

```bash
uv run nyanpasu memory-rebuild SOURCE_TASK_ID
```

The command waits for publication, reuses a completed extraction checkpoint when available, and exits unsuccessfully on failure. Maintenance status, errors, and native history remain visible as ordinary Dashboard tasks.

Upgrades combine existing task accounts for the same audience, context, and generation in order, preserving their contents and keeping old source IDs readable as references to the combined account. Migrated accounts may temporarily exceed 16,000 characters until their next extraction update. Existing summaries become stale when their source revisions change; background consolidation or the following command refreshes them:

```bash
uv run nyanpasu memory-rebuild --all
```

This rebuilds summaries from the current session accounts without re-extracting native sessions. Changing the extraction guidance does not rewrite existing accounts: both rebuild commands reuse completed extraction receipts, and unchanged source digests also allow existing summaries to be reused. Prompt changes alone do not invalidate either result. Rebuilding summaries cannot recover feedback omitted from those accounts; recovering it requires fresh extraction from the original evidence, which these commands do not force for completed accounts.

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

To allow remote access, set a random bearer token and the listening address:

```toml
[server]
host = "0.0.0.0"
port = 8765
token = "replace-with-a-random-token"
```

Generate a token with `uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
Alternatively, set `NYANPASU__SERVER__TOKEN` in the service environment; it overrides `server.token`.
An empty token is rejected. Omitting both settings keeps local unauthenticated access available.
Restart the service after changing the token or listening address. Use HTTPS through a reverse
proxy when accessing the service over an untrusted network.

The Dashboard asks for the token and stores it only in browser local storage. **Sign out** removes
it and clears the displayed data. All `/api/*`, `/tasks`, `/contexts`, and plugin routes require
`Authorization: Bearer <token>` when configured, including exports and content downloads. Tokens
are never accepted in query strings. `/dashboard`, its static assets, and `/health` remain public.
For example, `curl -H "Authorization: Bearer $NYANPASU__SERVER__TOKEN" http://127.0.0.1:8765/tasks`.

Plugin routers inherit this authentication by default. A plugin with its own authentication may
register a router with `require_auth=False`. The GitHub reviewer does this only when
`webhook_secret` is configured, so GitHub deliveries use their HMAC signature instead of the
Dashboard token. Without a webhook secret, that endpoint requires the server token too.

Each backend owns its native conversation history. Nyanpasu stores scheduling metadata and session references; the Dashboard reads native messages, reasoning, tool calls, edits, and results without maintaining another conversation database. Session details identify the backend and native session ID. Historical conversations remain readable after changing backends while their runtime and history files remain available.

The session list shows root sessions by default; enable **Show subtask sessions** to include children. Each session has **Conversation**, **Session details**, and **Sub tasks** tabs. Switching tabs preserves reading position, search input, and expanded task groups; the selected tab can be bookmarked. Subtask cards use consistent colors for independent design, module review, and test audit, with text labels and separate status indicators. They include nested children, waiting relationships, result summaries, and downloadable evidence. Open a child conversation and use **Back to parent session** to return to your reading position.

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

Run `pnpm run fmt` to format the frontend, browser tests, and helper scripts, or `just fmt` to also format Python and Markdown. `just ci-fmt-check` checks the same formatting scope without changing files.

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

The plugin can also start its poller during plugin setup. GitHub reviewer polling combines repository events, PR state polling, and PR timeline polling into one event journal: the first poll records current cursors and PR snapshots without handling older work, later polls process matching events after those cursors, and already admitted logical events are skipped across webhooks and polling. `poll_max_events_per_cycle = 0` means dispatch every matching journal event in the poll window; set it to a positive number only when you intentionally want a per-cycle cap.

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
    kind="my_plugin.handle",
    execution_override=ExecutionOverride(backend="codex", reasoning="high"),
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

Task merging is opt-in with a `coalesce_key` and requires a registered preparer. Compatible queued tasks with the same key, plugin, context, execution target, and memory audience can merge within `runtime.coalesce_window_seconds`; the preparer owns their domain-specific merge. Ordinary tasks remain separate. Recording and merging happen in one transaction, and a running task cannot receive late merged events. This window does not delay task execution or guarantee that nearby events will share a turn.

By default, the task uses `workspace_policy = "context"`: Nyanpasu resets the context workspace to `workspace.revision` or `workspace.ref`, runs the selected backend there, and keeps that workspace for the next event in the same context. Plugins can opt into `workspace_policy = "event_snapshot"` only when they need a disposable per-event workspace.

### Managed subtasks and independent review

An agent can create, inspect, await, cancel and complete owned subtasks using the per-turn control command. `runtime.concurrency` counts root executions: descendants share the root slot, including while the parent waits. Closing a context stops and reclaims its descendants; frozen evidence remains available from task details in the Dashboard.

Before deep review, the reviewer uses `review-scope` to inspect a pinned Git file inventory and submit an admission decision for every changed path. The service rejects missing/duplicate paths and prevents any child role from taking relocated, undecided or out-of-parent scope. Decisions survive restart and freeze after child dispatch. The dashboard shows accepted, relocation and clarification groups; deferred paths never count as reviewed-clean. Useful acceptance evidence can live in a fixed PR archive without becoming repository-maintained code. The agent judges necessity and may read context across boundaries; the gate controls subtask assignments, not arbitrary file access by the agent.

The GitHub reviewer chooses when independent design is useful. A design child gets a fresh repository exported from the pinned merge-base and original requirements, derives the minimum responsibilities and reusable base behavior, then builds a failure model. The parent compares designs and audits production/test necessity, including concrete deletion, consolidation or replacement alternatives and reasons to retain them. It also checks whether tests catch realistic failures without breaking on behavior-preserving changes. The review dashboard requires necessity records before deep completion; it does not require a quota of simplification findings. The independent-design child has memory disabled; the core execution access controls also apply.

See [the provisional reviewer evaluation cases](evals/reviewer/README.md).
