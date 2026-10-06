# Reconcile deep evidence

First verify that the general review checkpoint has been delivered and that the PR is still open at the analyzed head. Preserve that result while investigating deep evidence. When using an independent reference, verify its source, requirement digest, independence disclosure, and validation evidence. Keep both designs at the same base and requirements. Latest-target integration is a separate comparison applied equally to both. Without a reference, derive and label the minimum responsibilities from requirements and base callers; the necessity audit still applies.

Compare semantic responsibilities, not raw line diff. Map requirements and invariants to each design's ownership, state, data flow, boundaries, failure behavior and tests. Investigate both directions:

- Author-only machinery may be unnecessary, or may preserve compatibility, handle a real failure, or implement a requirement the reference missed. Trace callers and prove which.
- Reference-only machinery may expose missing behavior, or may be speculative complexity in the reference. Verify its necessity before proposing it.
- Shared choices may share a wrong assumption. Agreement is not evidence of correctness.

Apply reviewer.md's design-necessity criteria to the outstanding admitted production and test responsibilities, including scope carried forward without an earlier audit. Reuse supported decisions from general review instead of repeating completed checks. Preserve the repository admission plan: useful acceptance evidence does not justify moving deferred demos/experiment histories into the deep queue.

Consolidate child evidence into the existing `simplification.production` and `simplification.tests` records under review-output.md. Read the artifacts and check whether returned evidence supports or refutes the parent's candidate; a child's conclusion or agreement is not sufficient. Save experiment details locally and summarize only public-safe evidence on the dashboard.

For promising candidates, try a bounded deletion, consolidation or replacement against actual production/tests in a disposable workspace. Establish a clean baseline, then check externally observable contracts and relevant callers. Show how the alternative reduces authoritative states, synchronization points, concepts, dependencies or exceptional branches. Smaller line count or a passing toy model alone is insufficient. If an experiment is infeasible, use concrete caller/contract evidence and disclose the limitation; do not present a sketch as validated. A full alternative PR is unnecessary.

Reconcile the findings with the necessity decisions and publish under review-output.md. Correct or withdraw earlier candidates contradicted by caller tracing or behavior checks, including observations already present in the general checkpoint. Link evidence to the current head and actual changed lines. Do not turn the reference's extra machinery into required work without necessity evidence.

Finish the test audit using test-review.md. Deep completion requires reconciled production and test necessity records: `completed` with examined alternatives and decisions, or `skipped` with a concrete absence-of-applicable-scope reason. Zero simplification findings is valid; zero investigation is not. Unexamined assigned scope or essential evidence gaps require `incomplete`, even when all bugs have findings. Update the same dashboard and canonical threads without repeating unchanged general findings. Preserve the delivered general result if independence, requirements or essential validation remain insufficient.
