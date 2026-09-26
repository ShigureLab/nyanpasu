# nyanpasu-github-reviewer

GitHub pull request review plugin for Nyanpasu.

This package owns GitHub review behavior: webhook payload parsing, polling, PR state baselines, `gh-llm` review prompts, and GitHub-facing review policy. Shared GitHub config/workspace/signature helpers come from `nyanpasu-github`. The Nyanpasu core runtime only receives generic `AgentTask` objects.

Each PR maps to one Nyanpasu context key, so follow-up events reuse the same agent session and context worktree. The worktree is reset to the current PR head before each review task.

## Config

```toml
enabled_plugins = ["github_reviewer"]

[codex]
model = "gpt-6-astra"
reasoning_effort = "medium"

[plugins.github_reviewer]
github_login = "your-github-login"
review_language = "Chinese"
poll_enabled = true
poll_interval_seconds = 600
poll_event_pages = 3
poll_max_events_per_cycle = 0
dry_run = false
post_reviews = true

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

`instruction_docs` are resolved when the review is prepared for execution. Plugin-level documents apply to every reviewer task; repo-level documents apply to that repo. They join the session's developer instructions rather than being appended to every user message. Configure these as trusted policy documents; PR content and comments remain external task material.

Choose the backend with `runtime.backend` and configure its `[codex]` or `[claude]` section; see [runtime configuration](../../README.md#runtime-configuration). Install the reviewer skills for the selected CLI and pass GitHub credentials through its `env` or `pass_env`. After switching backends and restarting Nyanpasu, the next review starts a new session on the selected backend, using its configured model and reasoning effort in the disclosure footer. Previous sessions remain available in history.

## Session Instructions And Turn Input

The fixed reviewer role is maintained in [reviewer.md](src/nyanpasu_github_reviewer/instructions/reviewer.md). It binds the PR identity, review boundaries, continuation rules, language, and skill usage to the agent session. Each execution renders its disclosure footer from the owning backend’s `model` and `reasoning_effort` configuration. If the model is unset, the footer names Codex or Claude Code without guessing a model. [review-output.md](src/nyanpasu_github_reviewer/instructions/review-output.md) is the reference for priorities, suggestions, review decisions, and footer placement. Tool procedures come from the `github-conversation` and `gh-slate` skills.

Every execution prepares one short user message containing the target head, worktree, publication mode, the current model's disclosure footer, and trigger summaries or request links. Supplying the footer in each turn also updates the declaration when an existing session resumes with a different model. The previous task head is a navigation hint, not proof that a review was completed. Submitted GitHub reviews and published threads remain the evidence for prior review coverage.

When publication is enabled, the agent resumes unfinished review and publication work before applying the silence rule. It may edit or replace its own unpublished review drafts after preserving their content and revalidating findings against the target head. Published discussions remain canonical. A review is complete only after its required publication is verified on GitHub; a completed task or updated dashboard alone does not prove publication succeeded.

The plugin prepares the task after the core acquires its context lease. It refreshes the PR from GitHub, checks that it remains eligible, and uses the same head for the workspace and turn input. Merged events preserve their request context without embedding other prompts. Events arriving while a task is running are handled by a later turn, which reads the context left by the preceding task.

General review runs alongside any independent-design or test-audit subtasks. The parent publishes its checked scope and verified findings before awaiting children or starting long deep experiments. Children return frozen evidence; the parent resumes to validate and integrate it into the same dashboard and canonical finding threads. A failed deep check preserves the delivered general result and records the remaining gap. The review workflow is agent-directed; the runtime schedules and resumes tasks but does not publish a checkpoint on the agent's behalf.

Independent design starts from minimum required behavior, base reuse and a responsibility/state map, then derives a failure model. Deep review audits the necessity of major production mechanisms and test families even when no reference child is needed. It records concrete simpler alternatives, remove/merge/replace/retain decisions, evidence and gaps; promising candidates get bounded deletion or replacement experiments against real code where feasible. A standalone model does not establish that a production layer is removable. Earlier correctness reviews alone do not close this scope on incremental follow-ups. There is no finding quota: justified retention is a valid outcome.

One root task and all descendants consume one concurrency slot, including while the root waits. Children never request additional root slots. With concurrency 1, other root reviews wait for this tree to finish; asynchronous deep review removes the dependency on delivering the general result, not the root capacity limit. New events for the same PR remain serialized to protect its workspace. A resumed parent rechecks the head before using old evidence or publishing.

## Run

When installed as a Nyanpasu plugin, `nyanpasu serve` starts the plugin and its poller if `poll_enabled = true`.

```bash
export NYANPASU_HOME="$HOME/.nyanpasu"
uv run nyanpasu serve
```

Nyanpasu always reads `$NYANPASU_HOME/config.toml`, defaulting to `~/.nyanpasu/config.toml`.

Webhook endpoint:

```text
POST /plugins/github-reviewer/webhook
```

Manual plugin commands:

```bash
uv run nyanpasu-github-reviewer poll --once
uv run nyanpasu-github-reviewer poll
uv run nyanpasu-github-reviewer review owner/repo 123
```

The poller combines repository events, PR state polling, and PR timeline polling into one event journal. The first run records the current cursors and snapshots without processing older work; later runs process filtered events after those cursors. Already journaled events and already processed delivery ids are skipped. `poll_max_events_per_cycle = 0` dispatches every matching journal event in the poll window; a positive value is an explicit per-cycle cap.

## Review Dashboard

The reviewer prompt directs the agent to use the `gh-slate` skill and CLI to maintain one dashboard named `nyanpasu-review` on each PR. Install the skill and **gh-slate 0.1.1 or newer** in the environment used by the selected agent, following the [gh-slate installation instructions](https://github.com/ShigureLab/gh-slate#install). For a uv tool installation, run `uv tool upgrade gh-slate`; verify `gh-slate --version` meets this minimum before deploying the reviewer template.

```toml
[codex.env]
GH_TOKEN = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }

[plugins.github_reviewer]
github_login = "your-bot-login"
```

The agent follows the skill to read existing slate data, preview changes, publish with the observed revision, and verify the result. The reviewer package ships one `review` profile in [templates/boards.toml](src/nyanpasu_github_reviewer/templates/boards.toml), with a [Jinja template](src/nyanpasu_github_reviewer/templates/review.md.j2), [JSON Schema](src/nyanpasu_github_reviewer/templates/review.schema.json), and [example data](src/nyanpasu_github_reviewer/templates/review.example.json). Each turn supplies the installed profile's absolute path, including resumed sessions; the agent must use it on first creation and reuse the embedded definition on follow-up reviews. A different existing definition is replaced with this profile on the next warranted update. Restart the service after upgrading the reviewer package.

The template requires gh-slate's `dictsort_natural` filter to display finding IDs in numeric order (`F1`, `F2`, …, `F10`) without changing IDs or canonical thread links. Upgrade the CLI before deploying this template.

The dashboard has Chinese headings and shows the analyzed head SHA, review status, separate general/deep stages, summary, and findings with priority, resolution status, canonical thread links, and optional rule-source links. Summaries and finding titles follow `review_language`. The statuses are `reviewing`, `preliminary`, `approved`, `changes_requested`, `comment`, and `incomplete`. `preliminary` delivers completed general results while deep work remains pending or running; an unfinished review never displays approval. Each stage records its own scope and outcome. Findings use stable object keys, and resolved or superseded entries remain in the table. The disclosure text comes from the current turn's model declaration. Detailed findings remain in their threads.

The **精简审查** section shows separate production and test assessments with expandable decisions and evidence. The profile schema requires both records when deep is completed: completed audits contain decisions, while skipped audits explain the absence of applicable scope. Pending or incomplete audits cannot satisfy deep completion. Nonblocking simplification findings use `kind: simplification`; existing findings without a kind still render normally. These checks enforce the report structure, not the truth or semantic coverage of the review. Existing dashboards retain their embedded definition until the next warranted update; that update adopts stages and rechecks missing necessity evidence rather than inventing past completion. A policy upgrade alone does not trigger public re-review.

Preview the bundled example from the repository root without writing to GitHub:

```bash
gh-slate render nyanpasu-review \
  --config packages/nyanpasu-github-reviewer/src/nyanpasu_github_reviewer/templates/boards.toml \
  --profile review \
  --data packages/nyanpasu-github-reviewer/src/nyanpasu_github_reviewer/templates/review.example.json
```

When a review warrants a visible update, the agent updates the dashboard while reviewing and again with the outcome before finishing. After earlier review and publication are verified complete, automatic follow-ups with no new code, evidence, finding status, decision, or explicit request leave it unchanged. `dry_run = true` or `post_reviews = false` prohibits all GitHub writes, including draft management and dashboard updates. Comments authored by the bot are ignored by event handling, preventing self-triggered review tasks.

Publication receipts and failures are available through the agent's tool output and final message in the session transcript. If the agent is interrupted before updating the dashboard, inspect its task record; the service does not publish on its behalf.

## Webhook-Like Polling Design

Webhook delivery is the reference behavior for this plugin. A GitHub webhook gives every event a concrete event type, action, payload, and delivery id. Polling must approximate that event stream before handing work to Nyanpasu; it should not directly dispatch raw poll results.

GitHub repository events are useful as a fast activity feed, but they are not a complete webhook replacement. They can be delayed and may miss fork pull-request head updates that are visible in the pull request timeline. For example, a fork PR can receive a new commit without a usable `PullRequestEvent` / `synchronize` item in the upstream repository events feed. The polling design therefore uses multiple sources and normalizes them into one internal event journal.

### Canonical Event

Every webhook or poll result is first converted into a canonical event:

```text
CanonicalGitHubEvent
- delivery_id
- source
- repo
- pr_number
- github_event
- action
- event_created_at
- dedupe_key
- payload_json
```

For real webhooks, `delivery_id` is GitHub's delivery id. For synthetic polling events, `delivery_id` is generated by Nyanpasu and `dedupe_key` is the stable uniqueness boundary. Examples:

```text
pull_request:synchronize:<repo>#<pr>:<old_head>-><new_head>
issue_comment:created:<comment_node_id>
pull_request_review_comment:created:<comment_node_id>
pull_request_review:submitted:<review_node_id>
```

The plugin writes canonical events to a journal before dispatch. The dispatcher is the only component that turns journal rows into `AgentTask` objects.

### Polling Sources

`repo_events_poll` reads GitHub repository events. It is a low-latency source for comments, reviews, closes, and some pull request state changes. It is not the source of truth for PR head changes.

`pr_state_poll` reads pull request metadata sorted by `updated` time and compares it with stored PR snapshots. Snapshot diffs generate webhook-like pull request events:

```text
new open PR after cold start baseline -> pull_request.opened
closed from open                     -> pull_request.closed
reopened from closed                 -> pull_request.reopened
draft true -> false                  -> pull_request.ready_for_review
head_sha changed                     -> pull_request.synchronize
title/body changed                   -> pull_request.edited
base_ref changed                     -> pull_request.edited or policy skip
```

This source is required to detect fork PR commits reliably. A changed `head_sha` should synthesize `pull_request.synchronize` even when repository events did not expose one.

`pr_timeline_poll` reads the timeline for PRs that changed recently. It generates canonical events for user-visible conversation:

```text
IssueComment                  -> issue_comment.created/edited
PullRequestReviewComment      -> pull_request_review_comment.created/edited
PullRequestReview             -> pull_request_review.submitted/edited
ReviewDismissedEvent          -> pull_request_review.dismissed
Commit                        -> auxiliary evidence for synchronize
```

Timeline commit items can help explain why a follow-up happened, but `pr_state_poll` remains the authoritative source for current head state.

### Cursors And State

Polling state should be explicit and restartable:

```text
github_repo_event_cursors(repo, last_event_created_at, cursor_event_ids)
github_pr_updated_cursors(repo, last_updated_at, pr_node_ids_at_same_ts)
github_pr_snapshots(repo, pr_number, node_id, state, draft, base_ref, head_ref, head_repo, head_sha, title_hash, body_hash, updated_at)
github_pr_timeline_cursors(repo, pr_number, last_item_created_at, node_ids_at_same_ts)
github_event_journal(dedupe_key unique, delivery_id, status, payload_json)
```

Timestamp cursors must also keep the ids seen at that timestamp. This avoids losing multiple GitHub events or PR updates that share the same second.

### Startup Semantics

On cold start, polling establishes cursors and PR snapshots without dispatching historical work. New events are only produced after the baseline.

On restart, polling resumes from the persisted cursors and snapshots. If multiple events for the same PR arrive between two polls, all canonical events are written to the journal in chronological order. The dispatcher may coalesce events for the same context into one agent turn, but the journal should still retain the individual event records for auditability.

### Dispatch And Coalescing

The dispatcher processes journal events with status transitions:

```text
pending -> running -> completed
pending -> running -> failed
pending -> skipped
```

`dedupe_key` prevents replaying the same logical event. The Nyanpasu core still owns per-context serialization, so events for the same PR cannot run concurrently in the same worktree.

Reviewer tasks opt into core coalescing. When several events are still queued for the same PR, the plugin prepares one task from their trigger summaries and the current GitHub PR state. A PR creation event and a synchronize event can therefore become one initial turn at the current head, regardless of arrival order. Coalescing never embeds complete task prompts and does not force an initial task into follow-up mode merely because another task is queued.

Automatic follow-up events should not post noise. If an existing unresolved thread already covers the issue and the new event adds no new evidence or decision, the review turn may be skipped or summarized internally. Explicit user mentions and review-thread replies still deserve a GitHub-visible response when relevant.

### Known Limits

Polling cannot perfectly reproduce every GitHub delivery. If a comment is created and deleted between poll cycles, the plugin may never see it. If many commits land between cycles, the review should compare the last reviewed head against the latest head rather than replaying every intermediate commit as separate agent turns.

The reliability goal is not byte-for-byte webhook replay. The goal is to avoid missing actionable final state changes, especially fork PR head updates, mentions, review-thread replies, and review decisions.
