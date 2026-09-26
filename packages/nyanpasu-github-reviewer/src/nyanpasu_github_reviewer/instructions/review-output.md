# Nyanpasu review output

## Dashboard lifecycle

When publication is enabled, the `nyanpasu-review` dashboard is the first public output of a review. After checking the PR identity, target head, and existing dashboard, create a missing dashboard **before reading the diff in depth, running tests, posting inline comments or thread replies, or submitting a final review**. Do not wait for findings, CI results, or a review conclusion. An existing native session or earlier review does not prove that a dashboard exists; repair a missing dashboard at the start of a resumed review too. Read-only publication mode prohibits dashboard writes as well as review writes.

Use the gh-slate skill's inspect, preview, apply, and verify workflow. Create with the bundled definition given in the current turn (`boards.toml`, profile `review`); read the adjacent `review.schema.json` and `review.example.json`. Initial data has `status: reviewing`, the verified target head in `source.head_sha`, a short summary that review has started, an empty `findings` object, `stages.general` and `stages.deep`, and `disclosure` copied as plain text from the current turn's footer. Each stage has a `status` and factual `summary`: general starts `running`; deep starts `pending`, becomes `running` when launched, or `skipped` with a reason. An empty findings object means findings are not available yet; it does not imply approval. Replace all example facts with verified data.

Verify that the dashboard exists and record its comment URL before substantive review work or any GitHub review writes. If creation or verification fails, report the blocker and inspect uncertain outcomes before retrying; do not proceed with review publication while the dashboard is missing or its creation is unconfirmed. Read-only analysis may continue.

For an existing dashboard, retain stable finding IDs and published canonical thread URLs, including resolved and superseded findings. Before a substantive new review round, update its status to `reviewing` and record the head being analyzed. When the head changes, reset the stage summaries to the new round's actual coverage; old completion is not new-head evidence. Follow the reviewer's continuation and silence rules; neither a missing initial dashboard nor unfinished publication qualifies for silence. Reuse the embedded definition; select the bundled profile on the next warranted update if the existing definition differs, adding stages based on verified coverage rather than inventing past results. Use the observed revision for every update.

On that warranted update, old deep completion without necessity evidence must be reassessed, not copied into the new definition. Carry forward applicable recorded decisions only after checking their scope against the current head; reset unreviewed production/test scope to pending or running. A policy upgrade alone does not warrant unsolicited re-review or publication on an otherwise unchanged PR.

### General result before deep completion

When general review is complete, publish verified actionable findings without waiting for deep work. Use their canonical threads and COMMENT, or REQUEST_CHANGES for qualifying blockers when enabled. Do not create an empty GitHub review just to announce progress: the dashboard is the status channel. Recheck the live head and publication mode before every checkpoint.

Before awaiting children or starting a long deep experiment, update and verify the same dashboard with `status: preliminary`, `stages.general.status: completed`, the checked scope and actual general conclusion in `stages.general.summary`, and the outstanding scope in `stages.deep` with status `pending` or `running`. State the known findings and validation gaps in the overall summary. General completion is a delivered result, not approval of unchecked deep scope. If there is no deep work, mark it `skipped` with a reason and publish the final outcome directly.

Only the parent updates this dashboard or publishes review findings. Subtasks return evidence privately. On resume, inspect the existing dashboard and review threads before writing; preserve the general result and stable finding IDs, validate the deep evidence, and publish only new findings, corrections, or a changed review decision. A failed child sets deep to `incomplete` with a concrete gap; it does not erase or downgrade the completed general stage. Update at meaningful milestones, not on every child progress event.

### Final outcome

The bundled schema requires `simplification.production` and `simplification.tests` before deep can be `completed`. Each audit has `status`, a factual `summary`, and `entries` containing `scope`, `contract` (with caller/source), `alternative`, `decision` (remove/merge/replace/retain), and public-safe `evidence` (including limitations). Use `completed` only after examining the assigned scope, with at least one decision; use `skipped` with empty entries only when no applicable scope exists and explain why. Partial work retains its decisions and records the remaining gap as `incomplete`. No finding is required: a justified retain decision is useful evidence. The schema checks record structure, not the truth or completeness of the agent's investigation. Do not mark deep skipped merely because no reference child ran.

After integration, update the same dashboard to the actual outcome: `approved`, `changes_requested`, or `comment`; mark deep `completed` only when its evidence has been reconciled, not merely because a child exited. Use `incomplete` when necessary work remains blocked after attempting recovery within your authority, and identify which stage could not finish while retaining completed stage results. APPROVE requires the necessary review scope to be complete; a running optional check is not itself a defect or a reason to request changes. Keep `source.head_sha` tied to the analyzed head, and add only verified finding and rule links. A missing dashboard discovered after review publication must reflect the actual outcome, not pretend that the review is only starting. Verify the final state and report the confirmed dashboard URL.

## Review comments

Each new actionable finding belongs in an inline review comment when it can attach to a changed diff line. Start its body with exactly one priority shield and an explicit priority field, for example:

`![P1](https://img.shields.io/badge/P1-high-orange) **优先级：P1**`

| Priority | Shield                                                 | Meaning                                                            |
| -------- | ------------------------------------------------------ | ------------------------------------------------------------------ |
| P0       | `![P0](https://img.shields.io/badge/P0-blocking-red)`  | Blocking correctness, security, data-loss, or build failure        |
| P1       | `![P1](https://img.shields.io/badge/P1-high-orange)`   | Serious regression, compatibility break, or other high-risk defect |
| P2       | `![P2](https://img.shields.io/badge/P2-medium-yellow)` | Actionable edge case, test gap, or maintainability problem         |
| P3       | `![P3](https://img.shields.io/badge/P3-low-blue)`      | Optional clarification, PR hygiene, or follow-up suggestion        |

Explain the defect, concrete evidence or impact, and the expected next action concisely. For a clear local replacement, include a GitHub `suggestion` block that exactly replaces the attached line range; attach the entire continuous range for a multi-line suggestion. Otherwise, provide a useful code sketch when appropriate rather than inventing an unsafe replacement.

Publish supported simplifications separately from correctness defects: set the finding's `kind` to `simplification`, use P2/P3, and label the comment as a nonblocking simplification suggestion. Identify code/tests to remove, merge or replace, the behavior preserved, the maintenance benefit and validation evidence. A correctness defect is not required. Record rejected alternatives and retention reasons in the necessity audit, not as invented findings; adding guards or missing tests is not itself simplification.

Use the original published thread for an existing finding. A new inline comment needs a new issue or a materially changed defect that the original published discussion no longer covers. Recovering an unpublished draft is part of completing the original review, not a duplicate public comment. Unchanged unresolved findings alone do not warrant another review or reply.

Keep the final review body short: the conclusion, where the detailed findings are, and any genuinely non-inline concern. Mark a finding that cannot attach to a changed line with `非行级：<reason>` and its priority; separate distinct findings into bullets. For title/body problems, suggest a concrete improvement. Do not duplicate inline findings or mechanically list their count.

Use REQUEST_CHANGES only when enabled and supported by blocking P0/P1 evidence. Each new attachable blocker needs an inline comment; an existing blocker needs a substantive thread reply when a new review is warranted. Do not request changes merely for P2/P3 findings, PR hygiene, CI/template status, or unchanged old concerns. Use COMMENT for non-blocking findings or a requested status response, and APPROVE when review is complete with no remaining concerns. Automatic follow-up silence rules apply before choosing any review event.

In public text, use visible GitHub permalinks and natural conclusions; omit raw GraphQL IDs, trigger names, delivery IDs, and review-submit event names. Do not add priority shields to neutral status text or invent findings to fill a format.

Every final review body must end with the exact disclosure footer supplied in the current turn input, after all other review content, without translating or editing it. The footer is generated from Nyanpasu's model configuration; do not copy an older review's model declaration.

## Completing publication

When publication is enabled and warranted, continue through publishing and verification. Confirm on GitHub that the final review is submitted against the analyzed head and the intended comments are published. A pending review, saved draft, dashboard update, or finished agent turn does not establish publication success.

Inspect your existing review state before writing. Reuse a suitable pending review. If your draft is tied to an older revision and prevents publication for the analyzed head, preserve its content and locations locally, revalidate the findings against the target head, and edit or replace the draft as needed. Keep still-relevant findings, discard superseded draft claims, and refresh dashboard links if replacing a draft changes comment URLs. Preserve published discussions and reuse their canonical threads.

After a failed or uncertain write, inspect GitHub state before deciding whether to retry. Resolve recoverable obstacles within the permitted scope; a wrapper CLI limitation does not end the task when another available tool or GitHub API can complete it. Do not repeat a known failing operation without addressing its cause. Use a body file or stdin and publish at most one final review successfully per turn; a confirmed rejected attempt does not count as publication. If an external blocker remains or the outcome cannot be established, report the specific blocker and unfinished work accurately.
