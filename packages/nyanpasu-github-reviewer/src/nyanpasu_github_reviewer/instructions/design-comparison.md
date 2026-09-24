# Compare two hypotheses

Verify the frozen reference's source, requirement digest, independence disclosure, and validation evidence. Keep the author's implementation and the reference at the same base and requirements. Latest-target integration is a separate comparison applied equally to both; do not mix a merge-base reference with a rebased author implementation.

Compare semantic responsibilities, not raw line diff. Map requirements and invariants to each design's ownership, state, data flow, boundaries, failure behavior and tests. Investigate both directions:

- Author-only machinery may be unnecessary, or may preserve compatibility, handle a real failure, or implement a requirement the reference missed. Trace callers and prove which.
- Reference-only machinery may expose missing behavior, or may be speculative complexity in the reference. Verify its necessity before proposing it.
- Shared choices may share a wrong assumption. Agreement is not evidence of correctness.

For each candidate simplification, show an explicit counterfactual: the same reachable behavior and contracts, with fewer authoritative states, synchronization points, concepts, dependencies, exceptional branches, or affected callers. When useful, remove the machinery in a disposable patch and execute the relevant behavior. Lower line count alone is insufficient. Record tradeoffs and validation gaps; the reference is never a golden answer.

Only publish a design finding when there is a concrete maintenance consequence and a feasible smaller alternative that preserves established requirements. Separate a supported correctness defect from a nonblocking design suggestion; personal naming/style preference is not a defect. Reject findings refuted by caller tracing or behavior checks. Link evidence to the current head and actual changed lines.

Finish the test audit using test-review.md. Reconcile design and test gaps before approval. If independence, requirements, or essential validation remain insufficient, report precisely that limitation and avoid claiming the comparison complete.
