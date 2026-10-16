# Review workflow

The parent owns review coverage, cross-scope reconciliation and publication. General/deep describe delivered progress, not mandatory rounds of investigation. reviewer.md supplies review criteria; review-output.md owns eligibility and publication; task control supplies operation schemas and manages child lifecycle and frozen results.

## Establish the work

Apply review-output.md's live-label check. Read the PR requirements, current requests and existing dashboard/review evidence. For continuations, use `pr view --after <previous fetched_at>` when available and expand triggering discussions and relevant human feedback. Address outstanding requests before applying silence; an unchanged head does not dispose of a request in a thread.

Compare the pinned inventory and current requirements with prior coverage. Resume unfinished work and reassess affected conclusions; reuse supported evidence for unchanged responsibilities. A previous task head is only a navigation hint. Completed review/publication with no new evidence, changed decision or outstanding request needs no automatic GitHub update. A policy upgrade alone does not reopen completed reviews.

Verify the worktree's pinned head and merge-base. Follow scope-review.md to submit placement decisions before bulk investigation or child dispatch; admission is not a completed review. Copy the returned source/scope to the dashboard. For stacks, review the direct-base diff and attribute inherited issues to their introducing layer.

Call `ci-refresh` and handle failures under ci-followup.md alongside review. Do not wait for pending CI or duplicate its broad tests. A CI-only turn follows ci-followup.md alone.

## Divide concrete questions early

Keep a compact `review-plan.json` with material questions, owners, evidence and gaps. Small cohesive reviews can stay with the parent. For multiple independent subsystem questions, create selected children as soon as admission is accepted, before doing their investigation yourself. The parent works on other questions and cross-scope interactions while they run.

Assign one primary investigator per question or experiment. Shared files can serve different questions; repeating the same investigation needs a verification reason. Supply available head-based parent/CI evidence with revision, command/job, result and limitations. Reconcile it before deciding to rerun anything.

Use managed task control:

| Role                 | Use                                                                                                                                                                                    |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scoped investigator  | A cohesive module or interaction question. Use a descriptive `purpose`, `kind: github_reviewer.review`, and a self-contained question, contract sources and evidence needed.           |
| Test auditor         | `purpose: test-audit` for a material detection or brittleness question needing separate investigation; test-review.md supplies criteria. A large test diff alone does not require one. |
| Independent designer | `purpose: independent-design` when an isolated requirements-based comparison can resolve a material design question, or when explicitly requested.                                     |
| CI analyst           | `purpose: ci-analysis` with the observed fingerprint under ci-followup.md; no admission scope or `review_files`.                                                                       |

For code-review children, copy accepted changed paths into `inputs.review_files`. Unchanged context files are readable but do not belong in that list. Head-based requests include the PR context, target head and owned question; children return concise conclusions, checked paths, evidence and gaps. The parent retains all undelegated coverage.

Serialize dispatch requests to a file and use the supplied control client. Success returns a managed task ID; a shell background ID is not a child. Inspect a failed or uncertain dispatch before retrying; reuse `request_key` only for the same input. Do not repeatedly wait on a shell command as if it were a running review child.

### Independent comparison, when selected

Record why the comparison is needed. No default run/skip/reuse assessment is required. Reuse only a reference with the same frozen base tree, requirements and constraints, citing its task/artifact identity; recheck integration when the target base changes.

Supply `inputs.reason` and `inputs.requirements`, each requirement having `text`, `source` and `provenance` (`explicit`, `base-contract` or `inferred`); optional inputs are `constraints`, `non_goals` and `unknowns`. The service replaces the prompt, exports the pinned merge-base into a fresh repository and freezes the manifest. Supply original observable requirements, label unknowns and disclose parental implementation exposure. Do not provide author code, tests or implementation-derived hints before the child freezes its reference. This is input isolation, not an OS/network sandbox.

## Deliver evidence as it becomes useful

Publish a verified actionable finding when ready; do not wait for remaining general questions, child results or a simplification pass. Update the dashboard at meaningful milestones with checked scope, supported conclusions and remaining questions. Keep general running while its initial coverage is unfinished. Use `preliminary` once a supported general result is delivered and identified deeper work remains. Do not submit an empty GitHub review to announce progress.

Before awaiting children or starting a long experiment, save the available conclusions and evidence in the existing plan and update the dashboard. No separate general-review report is required. If useful parent work remains, continue it. Otherwise call `await` for actual managed children and end the turn; the service resumes with frozen results. Do not poll or sleep. Pending CI alone is not a reason to await.

## Reconcile and finish

On resume, repeat eligibility checks and compare current range/requirements with frozen inputs. Cancel obsolete children and retain their evidence at its original identity. Reinvestigate affected conclusions without repeating unchanged work.

Read returned evidence and check that it answers the assigned question; independently verify a material claim where its evidence is insufficient or conflicts with another path. Use design-comparison.md only for a selected comparison or simplification candidate. Recover failed essential work or record its concrete gap. Do not add a necessity audit to close the review.

Finish when admitted coverage and material questions have supported conclusions. Deep is completed if selected deeper questions are reconciled, or skipped if no deeper investigation is needed. Neither status requires simplification records or invented retention decisions. Deferred admission concerns and essential evidence gaps remain visible and cannot become whole-PR approval.

Before final publication, recheck relevant requests and CI and require `review-verify` to match the reviewed range. A changed range needs a new run; never relabel old evidence. Follow review-output.md to choose the review event, recover drafts and verify publication. Only the parent publishes.
