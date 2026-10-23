# Repository admission

Call `review-scope` under review-workflow.md for the pinned changed-file inventory. It includes paths, sizes, binary markers and directory totals; renames are old-path removal plus new-path addition. Admission settles repository placement and child scope, not correctness or design necessity.

Group paths by responsibility and give each exactly one decision:

- `accept`: production code, durable regression tests, documentation/configuration or reusable examples serving the requested behavior or an established repository contract.
- `relocate`: material better kept elsewhere, with a concrete destination or retained replacement. One-off probes, experiment histories and standalone simulators may belong in PR artifacts; directory names or size alone do not establish this.
- `clarify`: a material placement or requirement question needs a maintainer decision. State the uncertainty without inventing a violation.

Use requirements and repository layout/rules to make this a brief placement pass. Read callers only when needed to settle an actual question. Group ordinary accepted changes with a concise reason/source; their required `alternative` field may explain why the current placement fits. Do not design replacements or prove retention for every mechanism before dispatching work. Required acceptance evidence and durable repository content are separate questions: external artifacts can preserve one-off evidence without adding a maintained simulator or historical dump.

Submit the current inventory ID and complete groups with the supplied schema. Use exact path tokens, not globs. For `path_encoding: percent`, decode with `urllib.parse.unquote_to_bytes` for filesystem access and preserve encoded tokens in decisions/assignments. The service rejects stale inventories, missing/unknown paths and duplicates; empty diffs use empty groups. Reconcile previous decisions against current requirements and submit them for this inventory. Code-review child dispatch freezes the plan for the run; CI and PR split analysis do not.

Copy the returned source/scope to the dashboard. Report a supported placement concern promptly with affected paths, consequence, destination and requirement/rule source. Keep relocated/uncertain paths visible as deferred, never reviewed-clean; they prevent whole-PR approval. Follow the normal publication and severity policy.

Code-review children receive accepted changed paths in `inputs.review_files`, bounded by the root plan and parent's assignment. A path appears once per request; siblings may share it for different questions. Other files remain readable as context without entering `review_files`. Independent designers receive only base plus requirements; the service withholds author paths.

Do not expand deferred material into a detailed audit. Check its evidence only as needed for an accepted production question, retaining mandatory integration/GPU evidence even when stored externally. Tests of a standalone simulator do not establish regression coverage of shipped code; trace the actual production boundary when that distinction matters.
