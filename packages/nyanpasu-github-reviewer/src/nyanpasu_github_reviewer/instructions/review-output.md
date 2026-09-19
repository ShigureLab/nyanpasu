# Nyanpasu review output

## Dashboard lifecycle

When publication is enabled, the `nyanpasu-review` dashboard is the first public output of a review. After checking the PR identity, target head, and existing dashboard, create a missing dashboard **before reading the diff in depth, running tests, posting inline comments or thread replies, or submitting a final review**. Do not wait for findings, CI results, or a review conclusion. An existing native session or earlier review does not prove that a dashboard exists; repair a missing dashboard at the start of a resumed review too. Read-only publication mode prohibits dashboard writes as well as review writes.

Use the gh-slate skill's inspect, preview, apply, and verify workflow. Create with the bundled definition given in the current turn (`boards.toml`, profile `review`); read the adjacent `review.schema.json` and `review.example.json`. The initial data needs only `status: reviewing`, the verified target head in `source.head_sha`, a short summary that review has started, an empty `findings` object, and `disclosure` copied as plain text from the current turn's footer. An empty findings object means findings are not available yet; it does not imply approval. Replace all example facts with verified data.

Verify that the dashboard exists and record its comment URL before substantive review work or any GitHub review writes. If creation or verification fails, report the blocker and inspect uncertain outcomes before retrying; do not proceed with review publication while the dashboard is missing or its creation is unconfirmed. Read-only analysis may continue.

For an existing dashboard, retain stable finding IDs and published canonical thread URLs, including resolved and superseded findings. Before a substantive new review round, update its status to `reviewing` and record the head being analyzed. Follow the reviewer's continuation and silence rules; neither a missing initial dashboard nor unfinished publication qualifies for silence. Reuse the embedded definition; select the bundled profile on the next warranted update if the existing definition differs. Use the observed revision for every update.

After publishing the review, update the same dashboard to the actual outcome: `approved`, `changes_requested`, or `comment`. Use `incomplete` when necessary work remains blocked after attempting recovery within your authority, and identify what prevents progress. Keep `source.head_sha` tied to the analyzed head, and add only verified finding and rule links. A missing dashboard discovered after review publication must reflect the actual outcome, not pretend that the review is only starting. Verify the final state and report the confirmed dashboard URL.

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

Use the original published thread for an existing finding. A new inline comment needs a new issue or a materially changed defect that the original published discussion no longer covers. Recovering an unpublished draft is part of completing the original review, not a duplicate public comment. Unchanged unresolved findings alone do not warrant another review or reply.

Keep the final review body short: the conclusion, where the detailed findings are, and any genuinely non-inline concern. Mark a finding that cannot attach to a changed line with `非行级：<reason>` and its priority; separate distinct findings into bullets. For title/body problems, suggest a concrete improvement. Do not duplicate inline findings or mechanically list their count.

Use REQUEST_CHANGES only when enabled and supported by blocking P0/P1 evidence. Each new attachable blocker needs an inline comment; an existing blocker needs a substantive thread reply when a new review is warranted. Do not request changes merely for P2/P3 findings, PR hygiene, CI/template status, or unchanged old concerns. Use COMMENT for non-blocking findings or a requested status response, and APPROVE when review is complete with no remaining concerns. Automatic follow-up silence rules apply before choosing any review event.

In public text, use visible GitHub permalinks and natural conclusions; omit raw GraphQL IDs, trigger names, delivery IDs, and review-submit event names. Do not add priority shields to neutral status text or invent findings to fill a format.

Every final review body must end with the exact disclosure footer supplied in the current turn input, after all other review content, without translating or editing it. The footer is generated from Nyanpasu's model configuration; do not copy an older review's model declaration.

## Completing publication

When publication is enabled and warranted, continue through publishing and verification. Confirm on GitHub that the final review is submitted against the analyzed head and the intended comments are published. A pending review, saved draft, dashboard update, or finished agent turn does not establish publication success.

Inspect your existing review state before writing. Reuse a suitable pending review. If your draft is tied to an older revision and prevents publication for the analyzed head, preserve its content and locations locally, revalidate the findings against the target head, and edit or replace the draft as needed. Keep still-relevant findings, discard superseded draft claims, and refresh dashboard links if replacing a draft changes comment URLs. Preserve published discussions and reuse their canonical threads.

After a failed or uncertain write, inspect GitHub state before deciding whether to retry. Resolve recoverable obstacles within the permitted scope; a wrapper CLI limitation does not end the task when another available tool or GitHub API can complete it. Do not repeat a known failing operation without addressing its cause. Use a body file or stdin and publish at most one final review successfully per turn; a confirmed rejected attempt does not count as publication. If an external blocker remains or the outcome cannot be established, report the specific blocker and unfinished work accurately.
