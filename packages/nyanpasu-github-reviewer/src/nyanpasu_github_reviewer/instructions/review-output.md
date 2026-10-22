# Nyanpasu review output

## Review eligibility

At the start of every turn, including resumed reviews and CI-only follow-ups, fetch the PR's current labels from GitHub, for example with `gh api repos/{repo}/issues/{pr_number} --jq '.labels[].name'`. Do this before review work, CI analysis, child dispatch, or any GitHub write. Labels can be added after PR creation; trigger payloads and earlier session state are not authoritative. Recheck live labels before each review or dashboard publication and before continuing after child results.

If the exact label `nyanpasu: skip-review` is present, stop this turn without further review work, child dispatch, or GitHub writes, including dashboard creation/updates, replies, reactions, and review submission. This takes precedence over explicit review requests, missing-dashboard repair, and unfinished review/publication. Preserve existing review state and report the skip reason only in the task result. If labels cannot be fetched, report the blocker privately and do not proceed. After the label is removed, a later review turn follows the normal workflow.

## Dashboard protocol

review-workflow.md owns review entry, investigation and delivery order. Use the existing `nyanpasu-review` dashboard for all review updates; an earlier session or review does not prove that it exists. Read-only publication mode prohibits dashboard writes as well as review writes.

Use the gh-slate skill's inspect, preview, apply, and verify workflow. Create with the bundled definition given in the current turn (`boards.toml`, profile `review`); read the adjacent `review.schema.json` and `review.example.json`. Initial data has `status: reviewing`, the complete pinned `source` from the current turn (head, direct base branch, merge-base and inventory ID), a short summary that review has started, an empty `findings` object, `stages.general` and `stages.deep`, and `disclosure` copied as plain text from the current turn's footer. Each stage has a `status` and factual `summary`: general starts `running`; deep starts `pending`, becomes `running` when launched, or `skipped` with a reason. An empty findings object means findings are not available yet; it does not imply approval. Replace all example facts with verified data.

Verify that the dashboard exists and record its comment URL before substantive review work or any GitHub review writes. If creation or verification fails, report the blocker and inspect uncertain outcomes before retrying; do not proceed with review publication while the dashboard is missing or its creation is unconfirmed. Read-only analysis may continue.

Retain stable finding IDs and published canonical thread URLs, including resolved and superseded findings. Reuse the embedded definition; select the bundled profile on the next warranted update if it differs, adding stages based on verified coverage rather than inventing past results. Use the observed revision for every update.

After a gh-slate upgrade, an older saved render can fail `state verify` with `rerender_mismatch` without visible edits. Inspect the saved data and dry-run with the current bundled profile; adopt it only when an update is warranted, then verify the resulting state.

### Repository admission data

Copy the service-returned `scope` report into the dashboard under scope-review.md. Show accepted, relocation and clarification counts plus reasons, sources and alternatives. Scope decisions describe repository admission, not finished correctness review. Keep deferred paths visible; a final COMMENT may report their scope concern alongside the accepted-scope review. Preserve the high-severity policy for REQUEST_CHANGES rather than inventing a blocker to report a scope objection.

### Stage data

General/deep describe delivered progress, not mandatory audits or finding categories. While initial coverage is unfinished, keep general `running` and publish supported conclusions in the summary/findings as they become available. Once a general result is delivered with identified deeper work remaining, use `status: preliminary`, general `completed`, and deep `pending` or `running`, with actual checked/pending scope in their summaries. A failed essential child leaves its stage `incomplete` without erasing delivered conclusions. Deep may be `skipped` when no further investigation is needed. Update at meaningful milestones, not every child progress event.

### Final outcome

`simplification` is optional and does not gate deep completion or approval. Include only actually investigated `production` and/or `tests` candidates; omit empty placeholders. Each included record has `status`, a factual `summary` and `entries` with `scope`, `contract`, `alternative`, `decision` (remove/merge/replace/retain) and public-safe `evidence` with limitations. A completed record contains at least one supported decision. Preserve existing records and their evidence identity, but do not reopen earlier reviews to populate missing audits. The bundled example illustrates optional records, not required work.

Final dashboard outcomes are `approved`, `changes_requested`, or `comment`. Use `incomplete` when necessary work remains blocked after attempting recovery within your authority, identifying the unfinished stage while retaining completed results. APPROVE requires the necessary review scope to be complete; a running optional check is not itself a defect or a reason to request changes. Keep `source.head_sha` tied to the analyzed head and add only verified finding/rule links. A missing dashboard discovered after review publication must reflect the actual outcome rather than pretend the review is starting. Verify the final state and record the confirmed dashboard URL.

### Optional CI evidence

The parent alone maintains the optional `ci` section. Read `ci-followup.md` for delegation and current-state verification. Only current failed checks belong here; omit `ci` entirely when no failures remain. Waiting, success, and recovery need no placeholder. CI is separate from review coverage: a CI update preserves the review `status`, `source`, `summary`, `stages`, `scope`, `simplification`, and `findings`, and never implies approval or a new code finding.

`ci.source.head_sha` identifies the current PR head and may differ from the last reviewed `source.head_sha`. Copy `ci.snapshot_fingerprint` from `ci-refresh`. Set `ci.status` to `analyzing`, `completed`, or `incomplete`; a failed child or unavailable evidence is incomplete, not a verified diagnosis. `ci.failures` is a nonempty object keyed by the service's stable failure IDs. Each entry includes the observed `name`, `url`, `conclusion`, `tested_sha`, and a factual `summary`; retain non-null `run_id` and `run_attempt` when supplied. Completed analysis requires public-safe `evidence` and `next_step`. If evidence is missing, use `incomplete` and explain the uncertainty or unavailable logs rather than inventing a cause. The template shows one compact table with job links, causes, evidence, and next steps; internal IDs and fingerprints remain metadata.

CI-only follow-ups do not require the admission inventory or general/deep review workflow. If no dashboard exists and current failures warrant one, use `status: not_reviewed`, a `source` containing only observed `head_sha` and `base_ref`, empty `findings`, and both stages `pending` with explicit unreviewed summaries. Do not fabricate a reviewed merge-base or inventory. Removing `ci` after recovery preserves this honest review state. A substantive code review later replaces it through the normal lifecycle with a complete pinned `source`. An existing completed review must never be reset to `not_reviewed` merely to report CI.

Before each CI publication, call `ci-refresh` and reconcile head, failure identities, tested SHAs, and attempts. Remove recovered/replaced entries; discard stale results. A failed observation is not success and must not erase known failures. Use the observed dashboard revision and verify the resulting comment. Do not create a missing dashboard for an empty CI snapshot or write anything for an unchanged snapshot with already recorded analysis.

## Rule sources

For a finding based on an explicit repository guideline, documented contract, or requirement, record `rule.title` and a verified public permalink in the dashboard finding. Link the actual applicable rule; a finding thread is not itself a rule source. General correctness evidence may have no corresponding written rule. Leave `rule` absent in that case; the template keeps the rule-source column and displays “—”. Do not invent a rule, cite a loosely related document, or drop an existing verified rule when updating a finding.

## Review comments

Each new actionable finding belongs in an inline review comment when it can attach to a changed diff line. Start its body with exactly one severity badge, explicit priority, and a short action title on the same line: `<picture>…</picture> **P1 · <action title>**`. Separate that heading from the explanation with a blank line.

Use the bundled `review.md.j2` as the single source for badge markup. Copy its complete `<picture>` fragment with the priority's severity substituted: dark and light SVG sources, the PNG fallback, meaningful alt text, and `width="62" height="18" align="texttop"`. This is the same badge used in dashboard finding tables. Do not use Markdown image syntax, a bare PNG, emoji severity indicators, or an image without explicit dimensions. Those variants lose theme support or render at an inconsistent size. Preview the dashboard and use the rendered fragment if needed; do not improvise another badge format.

| Priority | Severity badge | Meaning                                                                             |
| -------- | -------------- | ----------------------------------------------------------------------------------- |
| P1       | High           | Blocking correctness, security, data-loss, build failure, or other high-risk defect |
| P2       | Medium         | Actionable edge case, test gap, or maintainability problem                          |
| P3       | Low            | Optional clarification, PR hygiene, or follow-up suggestion                         |

Use only P1/P2/P3 for new findings. P0 is a legacy alias accepted only in previously published dashboard data and renders as High; do not emit it in new reviews.

Explain the defect, concrete evidence or impact, and the expected next action concisely. For a clear local replacement, include a GitHub `suggestion` block that exactly replaces the attached line range; attach the entire continuous range for a multi-line suggestion. Otherwise, provide a useful code sketch when appropriate rather than inventing an unsafe replacement.

For a supported simplification set `kind: simplification`, use P2/P3 and label it nonblocking. Identify the smaller alternative, preserved behavior, maintenance benefit and evidence. Keep rejected candidates in local notes; adding guards or missing tests is not itself simplification.

Match findings by root cause and affected contract, not wording or line number; reuse the canonical ID and published thread across revisions. A new inline comment needs a new issue or a materially changed defect the original discussion no longer covers. Distinguish new regressions from earlier misses when attribution matters. Recovering an unpublished draft completes the original review. Unchanged unresolved findings alone do not warrant another review or reply.

Keep the final review body short: a conclusion heading, one factual summary paragraph, the dashboard permalink, and any genuinely non-inline concern. The dashboard owns the grouped finding index; do not repeat inline findings, a severity inventory, or the full resolved history in the review body. Mark a finding that cannot attach to a changed line with `非行级：<reason>`, and use the same fixed-size badge and priority heading as inline findings; separate distinct findings into bullets. For title/body problems, suggest a concrete improvement.

Use REQUEST_CHANGES only when enabled and supported by unresolved or partially resolved P1 evidence. Each new attachable blocker needs an inline comment; an existing blocker needs a substantive thread reply when a new review is warranted. Do not request changes merely for P2/P3 findings, PR hygiene, CI/template status, or unchanged old concerns. Use COMMENT for non-blocking findings or a requested status response, and APPROVE when review is complete with no remaining concerns. Automatic follow-up silence rules apply before choosing any review event.

In public text, use visible GitHub permalinks and natural conclusions; omit raw GraphQL IDs, trigger names, delivery IDs, and review-submit event names. Do not add severity badges to neutral status text or invent findings to fill a format.

Every final review body must end with the exact disclosure footer supplied in the current turn input, after all other review content, without translating or editing it. Preserve the hyperlink on the Nyanpasu name in public review and comment footers. The footer is generated from Nyanpasu's model configuration; do not copy an older review's model declaration. Dashboard `disclosure` remains plain text starting with `Powered by Nyanpasu with `; the bundled template adds the same brand link, so do not store HTML or Markdown links in that field.

## Completing publication

When publication is enabled and warranted, continue through publishing and verification. Immediately before each review publication, run the task-control action `review-verify` and require its returned source to match the range reviewed. A changed head, retarget or changed merge-base requires a new review run; do not overwrite the old range identity to make stale evidence appear current. Confirm on GitHub that the final review is submitted against the analyzed head and the intended comments are published. A pending review, saved draft, dashboard update, or finished agent turn does not establish publication success.

Inspect your existing review state before writing. Reuse a suitable pending review. If your draft is tied to an older revision and prevents publication for the analyzed head, preserve its content and locations locally, revalidate the findings against the target head, and edit or replace the draft as needed. Keep still-relevant findings, discard superseded draft claims, and refresh dashboard links if replacing a draft changes comment URLs. Preserve published discussions and reuse their canonical threads.

After a failed or uncertain write, inspect GitHub state before deciding whether to retry. Resolve recoverable obstacles within the permitted scope; a wrapper CLI limitation does not end the task when another available tool or GitHub API can complete it. Do not repeat a known failing operation without addressing its cause. Use a body file or stdin and publish at most one final review successfully per turn; a confirmed rejected attempt does not count as publication. If an external blocker remains or the outcome cannot be established, report the specific blocker and unfinished work accurately.
