# Reviewer behavior evaluation

The runtime tests validate orchestration. They do not establish that a model identifies elegant designs or valuable tests. These cases are a small, provisional evaluation set; maintainer review and historical PR cases are still required before making quality claims.

Use the same model, effort, environment and original requirements for three conditions: existing review, independent design comparison, and independent failure model plus test audit. Give the independent child only `requirements` and `base`; disclose implementation exposure on continuations. Freeze its artifacts before revealing `head` and `tests`. Keep the grading notes separate from the agent's input. Preserve complete outputs, costs, latency, skipped stages and unavailable checks.

A maintainer judges each finding without seeing which condition produced it: correct reachable defect, useful simplification, acceptable tradeoff, unsupported preference, invalid deletion, or missing evidence. Repeat a subset to assess stability. Do not grade by keyword inclusion, report length, finding count, or the agent's own preference. These fixtures are deliberately small; no claim of general effectiveness follows from passing them.

| Case             | Files                          | Question                                                                         |
| ---------------- | ------------------------------ | -------------------------------------------------------------------------------- |
| Whitespace       | `cases.json`, `whitespace`     | Does the test isolate the whitespace-sensitive behavior?                         |
| Request ordering | `cases.json`, `latest-request` | Does the reviewer construct the actual late-response sequence?                   |
| Cached size      | `cases.json`, `cached-size`    | Can duplicate state be removed while preserving observed behavior?               |
| Required adapter | `cases.json`, `adapter`        | Does the reviewer preserve the real protocol boundary?                           |
| Batch export     | `batch-export`                 | Does it discover questions, investigate them, and preserve justified boundaries? |

The original four cases expose a specific behavior in the requirements and remain useful directed checks. [Batch export](batch-export/README.md) adds a neutral request and runnable base/head repositories, so the reviewer must choose which questions to investigate. Its evaluator-only probes and grading notes are separate from the input. Compare old and new guidance with fresh sessions, otherwise identical settings, and the same unhinted input. This comparison is separate from the three design/test conditions above.

Retain observable investigation evidence, not just the final report: the questions the reviewer raised without evaluator feedback, paths and consumers examined, experiments it selected, conclusions it revised or ruled out, related paths it followed, and remaining gaps. An independently discovered valid issue outside the proposed grading notes still counts. Do not require a fixed investigation sequence or a particular tool when another method provides sufficient evidence. Human grading should consider whether the evidence supports the conclusion and whether useful boundaries survive proposed simplification.

Run code experiments in disposable workspaces. Record a clean baseline, one plausible defect, and a behavior-preserving alternative when relevant. A syntax/import/environment failure is inconclusive. The directed runtime experiments used to validate this implementation are separate from model evaluation.
