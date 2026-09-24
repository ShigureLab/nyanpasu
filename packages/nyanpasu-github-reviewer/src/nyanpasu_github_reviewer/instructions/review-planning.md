# Independent design and test evidence

Read this before implementation review. First establish the dashboard as required by review-output.md. In a continuation you may already know the implementation; disclose that and give a fresh child the original requirements, never your previous design conclusions.

Record a brief decision in `review-plan.json` and the native conversation: `run`, `skip`, or `reuse`, the reason, source head, and requirement sources. The agent makes this decision; the service owns lifecycle, version pinning, scheduling, and cleanup.

- Run independent design for state/lifecycle changes, new abstractions, changed boundaries/protocols, complex algorithms, or unclear necessity of added machinery. Explicit requests for independent comparison require this stage.
- Skip mechanical edits or a narrow correction to an already validated design, with a concrete reason.
- Reuse only a frozen reference with identical source tree, requirements, and design constraints. Check the input manifest; a matching head or past task completion alone proves nothing. Cite the prior task and artifact hashes. A changed target base requires rechecking integration behavior.
- If original requirements are missing, list unknowns. Mark requirements inferred from the PR as inferred; they cannot justify claiming the author's extra behavior is unnecessary.

Use the Nyanpasu subtask control supplied for this turn. For independent design, send:

```json
{
   "action": "create",
   "input": {
      "request_key": "reference:<head>:<requirements-id>:1",
      "purpose": "independent-design",
      "prompt": "Independent design and failure model",
      "inputs": {
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

Use `await` and end the turn. The service resumes the same run and workspace with terminal child states and frozen evidence. Failure, cancellation, missing evidence, or contaminated inputs mean incomplete, never a successful comparison. A completed child is an alternative hypothesis, not a specification.

Once the reference is frozen, read `design-comparison.md` in this directory and compare it with the author's change. Read `test-review.md` for every substantive review. A separate `test-audit` child is useful for large suites or independent adversarial checks; small changes can be audited directly. Give a test child the frozen failure model, target head, scope, and evidence questions; it receives a fresh head workspace by default.

Only the root reviewer publishes. Verify live PR head, the recorded base assumptions, CI, unresolved coverage gaps, and canonical findings before publication. If the head changes, retain the evidence under its original identity and prepare a new review; do not relabel old findings or artifacts as current.
