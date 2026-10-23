# Selected simplification and design comparisons

Use only for a concrete maintenance concern or requested comparison. Select relevant cues below; they are not a checklist, score or required audit of every code/test family.

- **Contracts and errors:** repeated validation or fallback is a cue; a real trust boundary or required error semantics may justify it. Support removal with reachable callers and preserved error behavior.
- **State and lifecycle:** duplicate maintained state is a cue; durability, ordering or ownership may justify it. Establish a single owner or derivation preserving reachable retry, cancellation and recovery.
- **Boundaries and abstractions:** a pass-through layer or branching wrapper is a cue; extension points or external APIs may justify it. Establish a smaller boundary against real consumers and dependencies.
- **Migration and compatibility:** an apparently obsolete adapter or dual path is a cue; supported versions, stored formats or mixed-version rollout may justify it. Establish that no required consumer or stored data still needs the path.
- **Test detection:** copied logic, self-comparison or overlapping cases are cues; distinct risks, oracles or execution boundaries can justify similar tests. Establish remaining detection with contract evidence or a directed experiment under test-review.md.

For a selected alternative, trace actual contracts and callers; use a bounded deletion/replacement experiment only if it can settle a remaining question. Follow the local testing policy. Fewer lines or a standalone model cannot establish production machinery removable. Record the preserved behavior, maintenance benefit, evidence and limitations in the existing plan; withdraw contradicted candidates.

For an independent reference, verify its frozen source tree, requirement digest, independence and evidence. Compare ownership, responsibilities and failure behavior at the same base and requirements; check latest-target integration equally. Machinery present in only one design may represent a missed requirement or speculation; trace its contract before deciding.

Summarize confirmed, still-applicable proposals in the optional dashboard `simplification` record under review-output.md with `status: recommended`. A completed retain-only analysis is not a suggestion. The shared 改进建议 section displays actionable results separately from pending analysis; rejected and historical alternatives stay in the review plan. No finding quota or retention table is required.
