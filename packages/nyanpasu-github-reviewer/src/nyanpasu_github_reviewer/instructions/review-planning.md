# Independent design and test evidence

Read this after scope-review.md and before implementation review. Complete the program-validated repository admission plan before dispatching any child. Decide whether a fresh independent reference would resolve a material design uncertainty before reading the head when feasible. In a continuation you may already know the implementation; disclose that and give a fresh child the original requirements, never your previous design conclusions.

Record a brief decision in `review-plan.json` and the native conversation: `run`, `skip`, or `reuse`, the reason, source head, and requirement sources. Explain which uncertainty independent work would resolve and what evidence could change the decision. Update material questions as investigation progresses; do not create tasks merely to fill stages. The agent makes this decision; the service owns lifecycle, version pinning, scheduling, and cleanup.

- Run independent design when uncertainty about the minimum required responsibilities or their decomposition makes a fresh reference useful. State/lifecycle changes, new abstractions, changed boundaries/protocols and complex algorithms are reasons to consider it, not automatic task triggers. Explain what the reference could settle that direct caller/contract investigation would not. Explicit requests for independent comparison require this stage.
- Skip independent design for mechanical edits or a narrow correction to an already validated design, with a concrete reason. This does not skip the necessity audit of substantive production and test changes. Reuse earlier audit conclusions only for unchanged responsibilities with recorded scope, alternatives and retention evidence; earlier bug fixes or a completed task alone are insufficient. If that record is absent, include the outstanding full-PR scope even on an incremental review.
- Reuse only a frozen reference with identical source tree, requirements, and design constraints. Check the input manifest; a matching head or past task completion alone proves nothing. Cite the prior task and artifact hashes. A changed target base requires rechecking integration behavior.
- If original requirements are missing, list unknowns. Mark requirements inferred from the PR as inferred; they cannot justify claiming the author's extra behavior is unnecessary.

Use the Nyanpasu subtask control supplied for this turn. For independent design, send:

```json
{
   "action": "create",
   "input": {
      "request_key": "reference:<head>:<requirements-id>:1",
      "purpose": "independent-design",
      "prompt": "Minimum required design, simpler alternatives, and failure model",
      "inputs": {
         "review_files": ["accepted/changed/path.py"],
         "reason": "Why this change benefits from a reference",
         "requirements": [
            {
               "text": "Observable behavior required",
               "source": "Original request or base contract location",
               "provenance": "explicit"
            }
         ],
         "constraints": [],
         "non_goals": [],
         "unknowns": []
      }
   }
}
```

The service replaces the child prompt, pins head/target-base/merge-base and input digests, and exports the merge-base tree into a fresh repository. Do not smuggle class names, new tests, author implementation steps, or proposed abstractions into requirements. Preserve actual compatibility constraints. This is input isolation, not an OS/network security boundary.

Creating a child starts it asynchronously. Continue the general review in the parent immediately: inspect the head diff, trace callers and compatibility, examine existing findings and CI, and validate concrete correctness risks. Do not call `await` immediately after `create`, wait for a reference before reading the head, or poll child status while useful general review work remains. The child keeps its frozen inputs; never feed it implementation-derived hints before it freezes its reference.

Investigate design necessity in the parent and module reviews from the start, following reviewer.md. Use a managed generic subtask when a specific head-based deletion/consolidation experiment is long or benefits from a fresh investigator; small checks can stay in the parent. Give it the pinned head, `inputs.review_files`, contract sources, candidate alternative explicitly labeled as a hypothesis, and outcomes that would support or refute it. Supply self-contained `developer_instructions` to try to disprove the alternative, check real consumers and failure paths, and return supported/refuted/inconclusive evidence, a patch if used, commands/results and limitations through `complete`. Use `kind: github_reviewer.review` to reuse its configured execution settings and a descriptive `purpose`; do not add a task kind or use `independent-design` for a child that must inspect the head. This investigator can see your candidate and is not an isolated reference designer. Only the parent publishes.

Read `test-review.md` for every substantive review. General review includes the tests needed to support its findings and short necessity checks; unresolved comparisons and extended experiments belong to deep review, whether or not an independent-design child runs. A separate `test-audit` child is useful for large suites or independent adversarial checks; small changes can be audited directly. Give a test child the target head, scope, questions about both detection power and avoidable maintenance, and any already frozen failure model; otherwise it derives and labels its own. It receives a fresh head workspace by default. Do not delay general review to obtain a child's failure model.

Assign the production and test necessity scopes explicitly in `review-plan.json`, including those the parent owns. Ask module reviewers to investigate redundant authority, wrappers, branches and speculative compatibility within their scope, and test reviewers to investigate consolidation, replacement and deletion. Every assigned scope returns decisions and evidence even when no simplification is justified. The parent reconciles this evidence using `design-comparison.md`; children never publish it themselves.

As soon as general review is complete, save its checked scope, supported correctness and simplification findings, commands/results, and remaining questions in `general-review.md`. Publish and verify that checkpoint using the general/deep dashboard stages in `review-output.md`, before waiting for unfinished children or starting a long design comparison or test experiment. In read-only mode retain the same checkpoint locally and report it in the native conversation. General findings remain available if deep review later fails. If all necessary evidence, including production/test necessity decisions, is already reconciled, finish both stages in the same turn; a child or extra turn is unnecessary. Skip deep only when there is no applicable work, with a concrete reason.

Only after delivering that checkpoint, use `await` for unfinished children and end the turn with the initial result, confirmed dashboard URL, and pending scope. The service resumes the same task and workspace with terminal child states and frozen evidence. The task remains waiting, not completed; the parent and all descendants share one root concurrency slot throughout. A child finishing while the parent is still reviewing does not interrupt that turn.

On resume, inspect current PR state, head, requirements and relevant discussions before further experiments or writes. Compare current requirements and constraints with the plan and frozen child inputs even when the code inventory is unchanged. Reinvestigate affected conclusions and dispatch a fresh reference with revised requirements when needed; retain still-applicable evidence with its original assumptions. If closed or superseded, cancel obsolete children, retain the old-head evidence, and end this round without publishing it as current; normal event handling prepares the current head. Otherwise verify the general checkpoint and its publication, consume the child evidence, read `design-comparison.md`, and reconcile the deep findings. Do not rerun or republish unchanged general findings. Failure, cancellation, missing evidence, or contaminated inputs mean incomplete deep review, never a successful comparison; preserve the completed general stage. A completed child is an alternative hypothesis, not a specification.

Only the root reviewer publishes. Verify live PR head, the recorded base assumptions, CI, unresolved coverage gaps, and canonical findings before publication. If the head changes, retain the evidence under its original identity and prepare a new review; do not relabel old findings or artifacts as current.
