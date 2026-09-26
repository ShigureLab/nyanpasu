# Decide what belongs before deep review

First establish the dashboard. Then use the supplied task-control command with `{"action":"review-scope"}`. The service returns a pinned Git inventory: every changed path, line counts, binary markers and directory totals. Renames appear as old-path removal plus new-path addition. Inspect this inventory, original requirements, repository layout/rules and existing review coverage before bulk patch reads or dispatching children. Use targeted reads and caller searches to settle placement questions; directory names or size alone are not rejection rules.

A verified no-op follow-up still follows the silence rule; do not reopen an unchanged completed review solely to add this record. When substantive work is warranted, reuse prior admission evidence where responsibilities and requirements are unchanged, and submit the reconciled plan for the current inventory.

Ask two separate questions: does an artifact help demonstrate the change, and must the repository maintain it after this PR? Required acceptance evidence does not automatically belong in the source tree. Always consider retaining evidence on a fixed external archive/PR artifact while keeping only production behavior, durable regression tests and reusable user examples in the repository. Do not limit alternatives to keeping every experimental driver or replacing all verification with one smoke test.

Group the inventory by responsibility and give every path exactly one decision:

- `accept`: repository maintenance is justified by a production caller, real regression risk, user workflow or explicit repository requirement. Include documents/configuration/deletions needed by that responsibility. Explain the source and why an external artifact or smaller existing mechanism is insufficient.
- `relocate`: propose moving/removing this submission material; name a concrete destination or retained replacement. Task-specific experiment histories, verdict dumps, one-off probes and standalone simulators often belong with PR evidence. A new top-level directory alone is not proof.
- `clarify`: placement or required behavior needs a maintainer decision. State the uncertainty; do not silently accept it as necessary or assert an unsupported violation.

Submit the complete plan using the same control command:

```json
{
   "action": "review-scope",
   "input": {
      "inventory_id": "from the inventory",
      "groups": [
         {
            "files": ["path/from/inventory.py"],
            "category": "production",
            "decision": "accept",
            "reason": "The existing service calls this implementation; this behavior must ship.",
            "source": "Original requirement URL and existing caller location",
            "alternative": "Keeping it only as an external experiment would not implement the service contract."
         }
      ]
   }
}
```

Categories are `production`, `tests`, `examples`, `evidence`, `generated`, `vendor`, `other`. Use exact inventory path tokens, not globs. When `path_encoding` is `percent`, all paths are encoded losslessly: decode with `urllib.parse.unquote_to_bytes` for filesystem access, but keep the encoded tokens in decisions and assignments. The service rejects stale inventories, omitted/unknown paths and duplicates. Empty diffs use empty groups. Reuse decisions only after verifying unchanged responsibilities and requirements; submit them against this inventory even on follow-ups. Once a child is dispatched the plan is frozen for this run.

Copy the returned `scope` into the dashboard's `scope` field (update to the bundled profile when needed). It is keyed by the reviewed head; the template reads the entry for `source.head_sha` and rejects a stale report. Publish a concise scope concern before expensive investigation, with the affected paths, maintenance consequence, destination and rule/requirement source. Keep `relocate`/`clarify` paths visible as deferred, never reviewed-clean; do not claim whole-PR approval while they remain. Follow publication permissions and review-event policy; a placement concern does not fabricate a P1 runtime bug.

Only accepted files may be assigned to children: add `inputs.review_files` to each create request, including independent-design requests. The service checks all roles and nested children against the root plan and the parent's assigned files. Independent designers still receive only base plus requirements; the service keeps author paths out of their prompt. Other reviewers may trace cross-file context, but report only on their owned scope. Retain parent ownership of accepted files not delegated.

Continue general review of accepted scope and deliver its checkpoint without waiting for deep children. For deferred material, stop line-by-line polishing, mutation campaigns and duplicate design work. Verify specific evidence claims only as far as required to assess the production change, recording that purpose separately from repository admission. Do not quietly omit mandatory GPU/integration evidence because its files will live elsewhere. Report the scope concern even if all production behavior is correct.

For tests, establish what production code actually executes. A simulator plus tests of that simulator may be useful design evidence, but cannot justify retaining a second implementation as production regression coverage. Prefer a small test exercising the real entry point and an independent expected result; determine the test's intended purpose before asking for more assertions.
