# Redundancy and repository conventions

Look for unnecessary implementation and meaningful convention drift in the changed responsibilities. Select relevant cues; no score, finding quota or necessity audit of every code/test family is required.

- **Contracts and errors:** repeated validation or fallback is a cue; a real trust boundary or required error semantics may justify it. Support removal with reachable callers and preserved error behavior.
- **State and lifecycle:** duplicate maintained state is a cue; durability, ordering or ownership may justify it. Establish a single owner or derivation preserving reachable retry, cancellation and recovery.
- **Boundaries and abstractions:** a pass-through layer or branching wrapper is a cue; extension points or external APIs may justify it. Establish a smaller boundary against real consumers and dependencies.
- **Repository conventions:** compare related responsibilities against applicable rules and established patterns. A deviation needs a concrete rule or maintenance consequence; differences between layers and deliberate tradeoffs may be justified. Reuse existing patterns without adding layers merely for uniformity. Leave mechanical formatting to existing tooling.
- **Migration and compatibility:** an apparently obsolete adapter or dual path is a cue; supported versions, stored formats or mixed-version rollout may justify it. Establish that no required consumer or stored data still needs the path.
- **Test detection:** copied logic, self-comparison or overlapping cases are cues; distinct risks, oracles or execution boundaries can justify similar tests. Establish remaining detection with contract evidence or a directed experiment under test-review.md.

For each proposal, identify what to delete, merge, reuse or align, its maintenance benefit or applicable rule source, and evidence preserving required behavior. Trace actual contracts and callers; use a bounded experiment only for a remaining uncertainty. A behavior defect additionally needs a reachable trigger and consequence. Fewer lines or a standalone model cannot establish production machinery removable. Record evidence and limitations in the existing plan; withdraw contradicted candidates.

## Independent reference

Request a managed `purpose: independent-design` child when a requirements-based comparison can resolve a material question. Supply `inputs.reason` and `inputs.requirements`: each has `text`, `source` and `provenance` (`explicit`, `base-contract` or `inferred`); optional inputs are `constraints`, `non_goals` and `unknowns`.

Supply original observable requirements, label unknowns and disclose implementation exposure. The service exports the pinned base and freezes the manifest. Withhold author code, tests and implementation hints until the reference is frozen; isolation covers inputs, not the filesystem or network. Verify the manifest, independence and evidence. Reuse only for unchanged base, requirements and constraints. Compare responsibilities and failure behavior; machinery present in only one design requires contract evidence before removal.

## Publish a supported proposal

Summarize confirmed, still-applicable proposals in the optional dashboard `simplification` record under review-output.md with `status: recommended`. A completed retain-only analysis is not a suggestion. The shared 改进建议 section displays actionable results separately from pending analysis; rejected and historical alternatives stay in the review plan. No finding quota or retention table is required.
