# Local testing during review

CI owns suite runs. Across parent and children, run only a handful of individual cases when existing evidence cannot settle a material question. State the suspected failure, decisive outcome and exact case or parameter first. Reuse applicable checks while preserving independent-design isolation.

For a directed experiment, use the same selected cases before and after the change, or a minimal reproducer. Stop when the question is settled and verify the restored tree. Report the revision, command or CI job/attempt, environment, result and remaining gaps.
