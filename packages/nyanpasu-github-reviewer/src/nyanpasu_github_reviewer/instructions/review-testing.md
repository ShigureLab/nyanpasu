# Local testing during review

This policy applies to the parent reviewer and every review child, including test auditors, independent designers and CI analysts.

CI owns broad regression runs. Do not run entire test files, directories, changed-file suites or the full repository suite during review. Pending, unavailable or failed CI does not authorize duplicating those runs locally.

Run only a small handful of individually selected test cases across the review, and only to resolve a concrete question that source inspection or existing evidence cannot settle. Before running, state the suspected failure, which outcome would change the conclusion and the selected cases. Use exact pytest node IDs or unittest method names; select individual parameter values when parametrization would expand into many cases. Do not recreate a suite run through successive small batches.

Reuse matching CI logs and the parent's or another child's recorded checks when the assignment permits that context; independent designers must preserve their requirements/base-only isolation. Evidence must identify the source revision, command or CI job and attempt, result and relevant environment. A new run needs a remaining question or a material change to that evidence; another investigator having run it is not a reason to repeat it.

For a directed mutation or refactor experiment, establish the baseline using only the same selected cases and rerun only those cases after the change. A minimal reproducer may be used for a specific boundary that those cases cannot exercise. Keep it bounded and stop when the question is resolved. Do not launch a mutation campaign or run a final full-suite check after restoring the workspace; verify the tree is clean instead.

Report the selected evidence and its limits. Reading CI results or passing a few cases does not establish unexamined behavior; record an unavailable boundary as a gap rather than broadening local testing.
