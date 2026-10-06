# nyanpasu-github-reviewer

GitHub pull request review plugin for Nyanpasu.

This package owns GitHub review behavior: webhook payload parsing, polling, PR state baselines, `gh-llm` review prompts, and GitHub-facing review policy. Shared GitHub config/workspace/signature helpers come from `nyanpasu-github`. The Nyanpasu core runtime only receives generic `AgentTask` objects.

Each PR maps to one Nyanpasu context key. Follow-up events reuse its workspace and, while the backend and memory audience are unchanged, its native agent session. The worktree is reset to the current PR head before each review task.

## Config

```toml
[plugins]
enabled = ["github_reviewer"]

[backends.codex]
driver = "codex"

[backends.codex.defaults]
model = "your-review-model"
reasoning = "medium"

[tasks.kinds."github_reviewer.review".execution]
backend = "codex"

[plugins.settings.github_reviewer]
github_login = "your-github-login"
review_language = "Chinese"
poll_enabled = true
poll_interval_seconds = 600
ci_poll_interval_seconds = 120 # Set 0 to disable CI polling.
poll_event_pages = 3
poll_max_events_per_cycle = 0
dry_run = false
post_reviews = true

[[plugins.settings.github_reviewer.instruction_docs]]
name = "SOUL.md"
path = "/path/to/SOUL.md"

[plugins.settings.github_reviewer.repos."owner/repo"]
local_path = "/path/to/repo"
github_remote = "https://github.com/owner/repo.git"
base_branches = ["main"]

[[plugins.settings.github_reviewer.repos."owner/repo".instruction_docs]]
name = "AGENTS.md"
path = "/path/to/repo/AGENTS.md"
required = false
```

`instruction_docs` are resolved when the review is prepared for execution. Plugin-level documents apply to every reviewer task; repo-level documents apply to that repo. They join the session's developer instructions rather than being appended to every user message. Configure these as trusted policy documents; PR content and comments remain external task material.

Route reviews with `tasks.kinds."github_reviewer.review".execution` and configure the named backend under `backends.<name>`; see [runtime configuration](../../README.md#runtime-configuration). Install the reviewer skills for every selected CLI through its [native session home](../../README.md#native-session-homes). Agent credentials belong under `backends.<name>.process.env` or `process.pass_env`. Newly admitted reviews use the current routing; queued or interrupted reviews retain their admitted target. Previous sessions remain available in history.

Reviews use the repository's configured memory audience; the default reads public source summaries plus that repository's shared audience and contributes only to the shared audience. Independent-design children have memory disabled. Their default kind is `github_reviewer.independent-design`, which can select a different backend/model from the parent. See [background memory](../../README.md#background-memory) for access controls, extraction, and consolidation.

## Session Instructions And Turn Input

The fixed reviewer role is maintained in [reviewer.md](src/nyanpasu_github_reviewer/instructions/reviewer.md). It binds the PR identity, review boundaries, continuation rules, language, and skill usage to the agent session. Each execution renders its disclosure footer from the task’s resolved `model` and `reasoning` target. If the model is unset, the footer names Codex or Claude Code without guessing a model. [review-output.md](src/nyanpasu_github_reviewer/instructions/review-output.md) is the reference for priorities, suggestions, review decisions, and footer placement. Tool procedures come from the `github-conversation` and `gh-slate` skills.

The main reviewer builds and revises its understanding of required behavior, chooses investigations for material uncertainties, challenges returned evidence, and follows confirmed causes into related paths. It records questions and conclusions in the existing review plan and checkpoints. On continuation and before publication it rechecks requirements as well as code: `review-verify` protects the comparison range, not the meaning of an edited request. These are agent judgments; the runtime owns scope, input isolation and evidence lifecycle.

Every execution prepares one user message containing the target head, worktree, publication mode, the current model's disclosure footer, and trigger summaries with comment links and full bodies. Outstanding requests are addressed before silence or further code review; requests outside review policy receive an explanation when publication is enabled. Continuations use `gh-llm pr view --after <previous fetched_at>` for incremental timeline reads and expand the triggering discussions for context. Supplying the footer in each turn also updates the declaration when an existing session resumes with a different model. The previous task head is a navigation hint, not proof that a review was completed. Submitted GitHub reviews and published threads remain the evidence for prior review coverage.

PR discussion polling combines the issue timeline with the PR review comments endpoint, which supplies inline comments and replies missing from the issue timeline. Both sources share the existing cursor and event journal; webhook and poll deliveries of the same comment version are deduplicated.

When publication is enabled, the agent resumes unfinished review and publication work before applying the silence rule. It may edit or replace its own unpublished review drafts after preserving their content and revalidating findings against the target head. Published discussions remain canonical. A review is complete only after its required publication is verified on GitHub; a completed task or updated dashboard alone does not prove publication succeeded.

The plugin prepares the task after the core acquires its context lease. It refreshes the PR from GitHub, checks that it remains eligible, and uses the same head for the workspace and turn input. Merged events preserve their request context without embedding other prompts. Events arriving while a task is running are handled by a later turn, which reads the context left by the preceding task.

The `nyanpasu: skip-review` PR label pauses the agent's review work, including CI follow-ups and GitHub writes. The agent checks live labels at the start of each turn, before review or dashboard publication, and before continuing after child results, so labels added after PR creation also take effect. This policy overrides explicit review requests and unfinished publication. Removing the label allows the next review turn to proceed; label removal alone does not guarantee an immediate review.

General review runs alongside any independent-design or test-audit subtasks. The parent publishes its checked scope and verified findings before awaiting children or starting long deep experiments. Children return frozen evidence; the parent resumes to validate and integrate it into the same dashboard and canonical finding threads. A failed deep check preserves the delivered general result and records the remaining gap. The review workflow is agent-directed; the runtime schedules and resumes tasks but does not publish a checkpoint on the agent's behalf.

Independent design starts from minimum required behavior, base reuse and a responsibility/state map, then derives a failure model. Deep review audits the necessity of major production mechanisms and test families even when no reference child is needed. It records concrete simpler alternatives, remove/merge/replace/retain decisions, evidence and gaps; promising candidates get bounded deletion or replacement experiments against real code where feasible. A standalone model does not establish that a production layer is removable. Earlier correctness reviews alone do not close this scope on incremental follow-ups. There is no finding quota: justified retention is a valid outcome.

One root task and all descendants consume one concurrency slot, including while the root waits. Children never request additional root slots. With concurrency 1, other root reviews wait for this tree to finish; asynchronous deep review removes the dependency on delivering the general result, not the root capacity limit. New events for the same PR remain serialized to protect its workspace. A resumed parent rechecks the head before using old evidence or publishing.

### CI follow-ups

Code review starts on PR activity and does not wait for CI to finish. While polling is enabled, a separate lightweight loop observes every eligible open PR's current checks every `ci_poll_interval_seconds` (default 120 seconds; 0 disables this loop). It does not depend on a new commit, comment, or PR `updated_at` change. A newly completed failure or a change that removes/replaces a recorded failure queues a CI-only follow-up in the same PR context. Queued, running, successful, and unchanged checks do not create follow-ups. Checks on the current head and its current merge-test commit are associated with the PR, including fork PRs; Actions run attempts distinguish reruns at the same head. Failed GitHub reads preserve the previous observation.

The parent calls task-control `ci-refresh` at review start, before final publication, and before publishing CI evidence. Failures are delegated with `purpose: ci-analysis` and `inputs: {"fingerprint": "<observed fingerprint>"}`. The service pins the actual failure instances and supplies the child prompt; CI analysis does not require the code review admission inventory or `review_files`. The child reads bounded logs and necessary context, returns evidence privately, and does not fix, rerun, or publish CI. The parent alone updates the optional CI section in the existing review dashboard. A child failure leaves an explicit analysis gap; it does not downgrade completed code review stages.

Configure a fast model for this child independently from the reviewer. For the previously configured Claude-compatible provider alias:

```toml
[tasks.kinds."github_reviewer.ci-analysis".execution]
backend = "claude"
model = "deepseek-v4.1-flash-ali"
reasoning = "xhigh"
```

This requires a `claude` backend with provider credentials that serve the alias. CI-only parent turns retain the regular `github_reviewer.review` execution configuration and session. When ordinary review events and CI events coalesce, the parent performs the ordinary review and handles CI alongside it. Same-PR serialization and root concurrency limits still apply: a long active review may delay its queued CI follow-up. The observer does not reserve a task while CI is pending.

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

To explicitly request a review, include `/review` in a PR comment, inline review comment, or review body, just like mentioning the configured `@github_login`. The command is case-insensitive and must be a standalone token (for example, `please /review`); strings such as `/reviewer`, `docs/review`, and `https://example.com/review` do not match. New and edited comments are supported through both webhooks and polling. Comments and reviews authored by the configured agent itself are ignored.

Manual plugin commands:

```bash
uv run nyanpasu-github-reviewer poll --once
uv run nyanpasu-github-reviewer poll
uv run nyanpasu-github-reviewer review owner/repo 123
```

The poller combines repository events, PR state polling, and PR timeline polling into one event journal. The first run records the current cursors and snapshots without processing older work; later runs compare PR state and process timeline events after those cursors. Already journaled events and already admitted logical events are skipped across webhooks and polling. `poll_max_events_per_cycle = 0` dispatches every matching journal event in the poll window; a positive value is an explicit per-cycle cap.

### Stacked pull requests

GitHub stack metadata comes from the PR REST API; the service does not require a local `gh stack` installation or checkout. For a stack member, `base_branches` applies to the stack's trunk. For an ordinary PR it applies to the direct base. In both cases, the review diff uses the PR's direct base and its merge-base with the head. Session, worktree, coalescing and cleanup ownership remain per PR.

Discovery lists recent PRs without a direct-base filter and also scans every open PR page. The open scan is not limited by `poll_event_pages`, so old upper layers remain visible. Previously open snapshots missing from discovery are fetched individually, so closures still trigger cleanup after falling outside the recent page window, regardless of direct base or stack membership. State comparison includes stack membership and direct-base SHA changes for upper layers, even when the head and PR `updated_at` are unchanged. Moving the trunk alone does not schedule the whole repository for review. Timeline reads remain incremental and respect the target-branch policy.

Each task pins a review inventory, including the direct base ref and merge-base. Its `inventory_id` stays unchanged when the target advances without changing the effective diff. The Dashboard source and scope use that inventory ID so a retarget or changed merge-base cannot reuse a head-only scope record. The root reviewer calls the task-control action `review-verify` immediately before publication to recheck current eligibility and the comparison range. A mismatch requires a new review run; a recovered inventory from before range tracking also requires a new run. This verifies the observed GitHub state, not an atomic lock across a later GitHub write.

Existing dashboards retain their embedded definitions until the next warranted update selects the bundled profile. On that update, use the source supplied by the service and reconcile scope against the new inventory. A metadata-only update with unchanged scope and verified completed publication follows the normal silence rule. Stack review findings belong to the layer introducing them; approving a layer does not establish that the whole stack is ready to merge.

## Review Dashboard

The reviewer prompt directs the agent to use the `gh-slate` skill and CLI to maintain one dashboard named `nyanpasu-review` on each PR. Install the skill and **gh-slate 0.1.1 or newer** in the environment used by the selected agent, following the [gh-slate installation instructions](https://github.com/ShigureLab/gh-slate#install). For a uv tool installation, run `uv tool upgrade gh-slate`; verify `gh-slate --version` meets this minimum before deploying the reviewer template.

```toml
[backends.codex]
driver = "codex"

[backends.codex.process.env]
GH_TOKEN = { cmd = ["gh", "auth", "token", "--hostname", "github.com", "--user", "your-bot-login"] }

[plugins.settings.github_reviewer]
github_login = "your-bot-login"
```

The agent follows the skill to read existing slate data, preview changes, publish with the observed revision, and verify the result. The reviewer package ships one `review` profile in [templates/boards.toml](src/nyanpasu_github_reviewer/templates/boards.toml), with a [Jinja template](src/nyanpasu_github_reviewer/templates/review.md.j2), [JSON Schema](src/nyanpasu_github_reviewer/templates/review.schema.json), and [example data](src/nyanpasu_github_reviewer/templates/review.example.json). Each turn supplies the installed profile's absolute path, including resumed sessions; the agent must use it on first creation and reuse the embedded definition on follow-up reviews. A different existing definition is replaced with this profile on the next warranted update. Restart the service after upgrading the reviewer package.

The template requires gh-slate's `dictsort_natural` filter to display finding IDs in numeric order (`F1`, `F2`, …, `F10`) without changing IDs or canonical thread links. Upgrade the CLI before deploying this template.

The dashboard has Chinese headings and shows the analyzed head SHA, review status, separate general/deep stages, summary, and findings with priority, resolution status, canonical thread links, and optional rule-source links. Pending findings appear in an expanded table; resolved and superseded findings remain in a collapsed table. The five columns show the stable finding ID, severity badge, linked title with optional metadata, resolution status, and rule source. Severity cells contain the badge without a repeated priority label. Badge dimensions are fixed, and ID/status cells do not wrap. The head SHA is plain text so GitHub can link the commit automatically. Dashboard and review comments use the same 62×18 `<picture>` markup from the template, with SVG sources for dark and light themes. Summaries and finding titles follow `review_language`. The statuses are `not_reviewed`, `reviewing`, `preliminary`, `approved`, `changes_requested`, `comment`, and `incomplete`. `preliminary` delivers completed general results while deep work remains pending or running; an unfinished review never displays approval. Each stage records its own scope and outcome. The disclosure text comes from the current turn's model declaration; the Nyanpasu name links to the project repository in both review/comment and dashboard footers. Detailed findings remain in their threads.

The optional **CI 异常** table appears only while current failures exist. It shows their check version, job links, cause or uncertainty, evidence, and next steps. CI has its own source SHA and execution identities; adding, updating, or removing the section leaves existing review conclusions and coverage intact. No failures means no CI section, including after recovery or replacement by a new commit. If CI fails before a review dashboard exists, a `not_reviewed` dashboard records only the observed head/base and pending stages; it cannot claim coverage, findings, or approval. A later code review supplies the full pinned inventory. Recovery removes the CI section without deleting the review dashboard.

General/deep progress, repository scope, and simplification assessments use tables. The collapsed **提交范围** and **精简审查与验证依据** sections retain file lists, decisions, and evidence in their table cells; the latter shows separate production and test assessments. Rule sources remain a dedicated column: a finding that applies an explicit repository rule includes its verified title and URL; otherwise the cell shows “—”. The profile schema requires both records when deep is completed: completed audits contain decisions, while skipped audits explain the absence of applicable scope. Pending or incomplete audits cannot satisfy deep completion. Nonblocking simplification findings use `kind: simplification`; existing findings without a kind still render normally. These checks enforce the report structure, not the truth or semantic coverage of the review. Existing dashboards retain their embedded definition until the next warranted update; that update adopts stages and rechecks missing necessity evidence rather than inventing past completion. A policy upgrade alone does not trigger public re-review.

**TODO:** After all persisted dashboard findings using legacy P0 have been migrated to P1, remove P0 from the schema and template mapping.

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

For real webhooks, `delivery_id` is GitHub's delivery id. For synthetic polling events, `delivery_id` is generated by Nyanpasu. Both the poll journal and core task admission use the same `dedupe_key` rules. Examples:

```text
pull_request:synchronize:<repo>#<pr>:<new_head>
pull_request:opened:<repo>#<pr>:<head>
issue_comment:<comment_id>:<version_digest>
pull_request_review_comment:<comment_id>:<version_digest>
pull_request_review:<review_id>:<version_digest>
```

Comment and review versions include the body and available update/submission time, so new comments and later edits remain separate requests. Timeline delivery ids also include the item update time. Events without a shared occurrence identity, including repeated reviewer requests and CLI manual reviews, retain their delivery identity rather than collapsing all requests on the same PR head.

The poller writes canonical events to a journal before dispatch. Webhooks submit directly; both paths construct `AgentTask` objects with the shared event dedupe key.

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
stack membership/trunk changed       -> pull_request.edited or policy skip
upper-layer base_sha changed         -> pull_request.edited, even without updated_at changes
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
github_pr_snapshots(repo, pr_number, node_id, state, draft, base_ref, base_sha, stack_json, head_ref, head_repo, head_sha, title_hash, body_hash, updated_at)
github_pr_timeline_cursors(repo, pr_number, last_item_created_at, node_ids_at_same_ts)
github_event_journal(dedupe_key unique, delivery_id, status, payload_json)
```

Timestamp cursors must also keep the ids seen at that timestamp. This avoids losing multiple GitHub events or PR updates that share the same second.

### Startup Semantics

On cold start, review polling establishes cursors and PR snapshots without dispatching historical review work. New review events are only produced after the baseline. CI observation inspects current state immediately: existing current failures can create a CI-only follow-up, while an all-green baseline is silent. It does not replay historical failed runs.

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
