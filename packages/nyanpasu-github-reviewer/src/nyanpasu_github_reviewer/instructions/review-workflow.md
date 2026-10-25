# Review workflow

The parent owns review coverage, cross-scope reconciliation and publication. General/deep describe delivered progress, not mandatory rounds of investigation. reviewer.md supplies review criteria; review-output.md owns eligibility and publication; task control supplies operation schemas and manages child lifecycle and frozen results.

## Establish the work

Apply review-output.md's live-label check. Read the PR requirements, current requests and existing dashboard/review evidence. For continuations, use `pr view --after <previous fetched_at>` when available and expand triggering discussions and relevant human feedback. Address outstanding requests before applying silence; an unchanged head does not dispose of a request in a thread.

Compare the current delta and requirements with the last verified coverage. Continue unresolved questions and affected contracts; reuse evidence for unchanged behavior. Separate new regressions, misses in earlier coverage, and already known issues; a pre-existing base defect is not a new regression. A previous task head is only a navigation hint. Completed publication with no new evidence, changed decision or outstanding request needs no automatic update. A policy upgrade alone does not reopen it.

Verify the worktree's pinned head and merge-base. Follow scope-review.md to submit placement decisions before bulk investigation or code-review child dispatch; admission is not a completed review. Copy the returned source/scope to the dashboard. For stacks, review the direct-base diff and attribute inherited issues to their introducing layer.

During an eligible substantive review, dispatch one managed `purpose: pr-split` child when the turn input reports the size trigger (strictly more than 2000 added + deleted lines), or when the behavior changes clearly separate into multiple independently reviewable PRs, regardless of size. Put the trigger, candidate behavior boundaries and relevant requirements in `prompt`; omit `inputs` and `developer_instructions`. The service supplies the entire pinned direct-base delta, independent of admission, as `github_reviewer.pr-split`. Reuse an active child or completed result only for the same inventory and unchanged requirements; cite it in the review plan. Do not reopen an already completed, otherwise unchanged review merely to populate this advice. CI-only turns never trigger split analysis.

Call `ci-refresh` and handle failures under ci-followup.md alongside review. Do not wait for pending CI or duplicate its broad tests. A CI-only turn follows ci-followup.md alone.

## Divide concrete questions early

Keep material questions, owners, evidence and gaps in the existing `review-plan.json`. Track candidates by root cause and affected contract, reusing canonical finding IDs once published. Small cohesive reviews can stay with the parent. Delegate independent questions after admission, before investigating them yourself; work on other questions and cross-scope interactions meanwhile.

Assign one primary investigator per question. On follow-up, dispatch only investigations affected by the delta, unresolved evidence or changed contracts. Shared files can serve different questions. Supply matching parent/CI evidence with revision, command/job, result and limitations; repeating a check needs a remaining uncertainty.

Use managed task control:

- Scoped investigator: a cohesive question with a descriptive `purpose` and `kind: github_reviewer.review`.
- Test auditor: `purpose: test-audit` for a material detection or brittleness question under test-review.md; diff size alone does not require one.
- Independent designer: `purpose: independent-design` for a material requirements-based comparison or an explicit request.
- CI analyst: `purpose: ci-analysis` with the observed fingerprint under ci-followup.md; no admission scope or `review_files`.
- PR split analyst: `purpose: pr-split` for the trigger above; no admission scope or `review_files`. It advises on behavior boundaries, dependencies and the contributor's submission mode without owning correctness coverage.
- Related PR analyst: `purpose: pr-compare` when another PR appears to address the same behavior; `inputs.related_prs` lists its same-repository PR numbers. Put the suspected overlap and requirements in `prompt`; no `review_files`. The service pins both sides; the child compares implementation preference, current-PR changes, dependencies and replacement options without owning correctness coverage.

The plugin supplies child role instructions; omit `developer_instructions`. Put the concrete question, contract sources, available evidence and task-specific constraints in `prompt`; independent design uses the structured inputs below instead. Investigator and test-audit inputs accept only `review_files`.

For code-review children, copy accepted changed paths into `inputs.review_files`. Unchanged context files are readable but do not belong in that list. The service adds the pinned comparison range and owned paths to head-based requests; children return concise conclusions, checked paths, evidence and gaps. The parent retains all undelegated coverage.

Serialize dispatch requests to a file and use the supplied control client. Success returns a managed task ID; a shell background ID is not a child. Inspect a failed or uncertain dispatch before retrying; reuse `request_key` only for the same input. Do not repeatedly wait on a shell command as if it were a running review child.

For related PRs, use `{"action":"create","input":{"request_key":"compare:<current-inventory>:<related-heads>","purpose":"pr-compare","prompt":"Compare the overlapping behavior and propose coordination","inputs":{"related_prs":[447]}}}` with actual identities and PR numbers. Group connected candidates in one child; reuse an active or frozen result only while all compared ranges and material requirements are unchanged. Similar names or shared files alone need no child. Do not reopen an unchanged completed review solely for this policy; a new overlapping PR, changed related implementation or explicit comparison request is new evidence. CI-only turns do not launch comparisons.

### Independent comparison, when selected

Record why the comparison is needed. No default run/skip/reuse assessment is required. Reuse only a reference with the same frozen base tree, requirements and constraints, citing its task/artifact identity; recheck integration when the target base changes.

Supply `inputs.reason` and `inputs.requirements`, each requirement having `text`, `source` and `provenance` (`explicit`, `base-contract` or `inferred`); optional inputs are `constraints`, `non_goals` and `unknowns`. The service replaces the prompt, exports the pinned merge-base into a fresh repository and freezes the manifest. Supply original observable requirements, label unknowns and disclose parental implementation exposure. Do not provide author code, tests or implementation-derived hints before the child freezes its reference. This is input isolation, not an OS/network sandbox.

## Deliver evidence as it becomes useful

Publish a verified actionable finding when ready; do not wait for remaining general questions, child results or a simplification pass. Update the dashboard at meaningful milestones with checked scope, supported conclusions and remaining questions. Keep general running while its initial coverage is unfinished. Use `preliminary` once a supported general result is delivered and identified deeper work remains. Do not submit an empty GitHub review to announce progress.

Before a long experiment or wait, save evidence in the existing plan and update the dashboard; no separate checkpoint report is required. Continue useful parent work first. Then call `await` with the needed managed `task_ids`: `mode: any` for independently useful results, or `mode: all` when the decision needs them together. End the turn after either call, including when results are already ready. Pending CI alone is no reason to await; do not poll or sleep.

## Reconcile and finish

On resume, repeat eligibility checks and compare current range/requirements with frozen inputs. Cancel obsolete children and retain evidence at its original identity. Read delivered results, reconcile their claims and continue the remaining work. After `any`, other children remain active: await remaining needed results, excluding reconciled children from the next wait, then end that turn. A failed or cancelled child leaves a gap; recover essential work or record the blocker.

Reconcile split advice against behavior and dependency evidence, then publish it in the optional dashboard `pr_split` section under review-output.md. Await a dispatched split child before ending its analysis, while continuing to deliver useful review findings. A failed split child leaves that section `incomplete`; it does not change correctness stages or create a blocker. Recheck `review-verify` before publishing split advice, remove stale advice when replacing the dashboard source, and avoid repeating unchanged suggestions.

Await dispatched PR comparisons and reconcile their implementation, overlap and dependency claims. Verify each related PR's live state, head, base, merge-base and material requirements before reusing or publishing advice; changed evidence requires a fresh comparison, not relabeling the old result. Publish useful coordination in `pr_comparison` under review-output.md, including maintainer choices for full replacement. A failed comparison leaves that section `incomplete` without changing correctness stages. Clear stale comparison advice when replacing the current source; CI-only updates preserve it. Only the parent publishes, and unchanged advice needs no extra comment.

For high-risk candidates or conflicting evidence, obtain a fresh scoped investigator's falsification before accepting the claim; reuse it while its inputs remain valid. Use the ordinary investigator role, with the claim, sources, evidence and a question that could disprove its trigger, contract or consequence in the prompt. Do not prescribe a verdict. Otherwise, a straightforward finding with sufficient evidence needs no repeated counter-review. Reconcile contradictions before publication; agreement alone adds no proof.

If successive rounds merely restate symptoms or trade one symptom for another, revisit the root cause, sourced requirements and responsibility boundary. Seek a discriminating example or the missing decision instead of repeating equivalent assignments. Keep unresolved material gaps visible; reaching a round count never justifies closure or approval.

Finish when admitted coverage and material questions have supported conclusions. Deep is completed if selected deeper questions are reconciled, or skipped if no deeper investigation is needed. Neither status requires simplification records or invented retention decisions. Deferred admission concerns and essential evidence gaps remain visible and cannot become whole-PR approval.

Before final publication, recheck relevant requests and CI and require `review-verify` to match the reviewed range. A changed range needs a new run; never relabel old evidence. Follow review-output.md to choose the review event, recover drafts and verify publication. Only the parent publishes.
