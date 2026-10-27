# Review workflow

The parent owns review coverage and cross-scope reconciliation. reviewer.md supplies investigation criteria; scope-review.md owns admission; review-output.md owns eligibility, progress display and publication. The injected task-control instructions own operation schemas, waiting, retries and child lifecycle.

## Establish or resume the work

Apply review-output.md's eligibility check at every entry. A CI-only turn follows ci-followup.md alone. Otherwise, read current PR requirements, requests and existing review evidence; use `pr view --after <previous fetched_at>` when available and expand relevant discussions. An unchanged head does not dispose of an outstanding request.

Verify the worktree's pinned head and merge-base. Compare the current delta and requirements with the last verified coverage and remaining questions, including contracts affected by fixes. Reuse evidence for unchanged behavior. Distinguish new regressions, earlier misses and known findings; a pre-existing base defect is not a new regression. A previous task head is only a navigation hint. With completed publication and no new evidence, changed decision or outstanding request, remain silent; a policy upgrade alone does not reopen review.

Follow scope-review.md to submit placement decisions before bulk investigation or code-review child dispatch. For stacks, use the direct-base diff and attribute inherited issues to their introducing layer. Establish the dashboard under review-output.md. Refresh CI under ci-followup.md alongside code review, without waiting for pending checks or duplicating broad tests.

Reconcile active and delivered children against the current range and requirements. Cancel obsolete work and retain its evidence at the original identity; do not relabel stale results. Resume remaining investigations under the same process below.

## Investigate and reconcile

Use the existing `review-plan.json` for material questions, owners, evidence and gaps. Track candidates by root cause and affected contract, retaining canonical finding IDs. Small cohesive reviews can stay with the parent. Delegate independent questions after admission, before investigating them yourself; assign one primary investigator per question and work on other questions and cross-scope interactions meanwhile. On follow-up, investigate the delta, unresolved evidence and affected contracts.

Use managed task control. The plugin supplies child role instructions; omit `developer_instructions`. Put the question, contract sources, evidence and constraints in `prompt`; independent design uses design-comparison.md's structured requirements instead. Supply evidence with revision, command/job, result and limitations; repeat a check only for a remaining uncertainty.

| Role                 | Select for                                                                                            | Input and scope                                                                                                                                                                |
| -------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Scoped investigator  | A cohesive question; descriptive `purpose`, `kind: github_reviewer.review`                            | Inputs contain only `review_files`: accepted changed paths owned by this child, under scope-review.md.                                                                         |
| Test auditor         | A material detection or brittleness question under test-review.md; not diff size alone                | `purpose: test-audit`; inputs contain only `review_files`.                                                                                                                     |
| Independent designer | A material requirements-based comparison or explicit request                                          | `purpose: independent-design`; read design-comparison.md for requirements, isolation and reuse.                                                                                |
| CI analyst           | Current failures under ci-followup.md                                                                 | `purpose: ci-analysis`; observed fingerprint, no admission scope or `review_files`.                                                                                            |
| PR split analyst     | Dispatch one for the size trigger reported in the turn input, or clearly independent behavior changes | `purpose: pr-split`; omit `inputs`. Put trigger, boundaries and requirements in `prompt`; the service supplies the entire pinned direct-base delta independently of admission. |
| Related PR analyst   | Dispatch when memory, discussion or code reveals another PR addressing the same behavior              | `purpose: pr-compare`; `inputs.related_prs`: same-repository PR numbers. Put suspected overlap and requirements in `prompt`; no `review_files`. The service pins both sides.   |

Code-review children return conclusions, checked paths, evidence and gaps for their assigned scope; the parent retains undelegated coverage. Group connected PR comparisons in one child; shared names or files alone do not establish overlap. Split and comparison advice do not own correctness coverage or reopen an otherwise unchanged completed review. Reuse matching active or frozen analyses only while their ranges and material requirements remain valid, citing them in the plan.

Read delivered evidence and reconcile claims against the assigned question and affected behavior. For high-risk candidates or conflicting evidence, obtain a fresh scoped investigator's falsification before accepting the claim. Supply the claim, sources and a question that could disprove its trigger, contract or consequence, without prescribing a verdict. Reuse valid evidence; straightforward supported findings need no repeated counter-review. Agreement alone adds no proof.

Continue useful parent work while children run, then use task-control's asynchronous wait. Reconcile each delivered result and continue remaining needed work. A failed or cancelled essential child leaves a gap to recover or report. If successive rounds restate symptoms or trade one symptom for another, revisit the root cause, sourced requirements and responsibility boundary; seek a discriminating example or missing decision.

## Deliver and finish

Follow review-output.md to publish supported findings as they become useful and preserve progress before long experiments or waits. Its dashboard stages describe delivered coverage; they do not prescribe investigation rounds.

Finish when admitted coverage and material questions have supported conclusions. Keep deferred admission concerns and essential evidence gaps visible; they cannot become whole-PR approval. Reaching a round count does not justify closure. Reconcile optional analyses under review-output.md without making their outcome a correctness blocker, then follow its final verification and publication protocol.
