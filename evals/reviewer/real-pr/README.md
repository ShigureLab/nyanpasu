# Historical PR replay

`python scripts/reviewer_eval.py` prepares historical inputs, records native task output, and summarizes independently adjudicated findings. It uses the existing `nyanpasu run-task` entry point. This is an offline review experiment: it does not exercise GitHub admission, event polling, review publication, or the complete reviewer plugin lifecycle.

Keep the source repository, evaluator material, and experiment output outside one another. Prepare one timeline step at a time. The staged repository contains only the selected base/head trees and two neutral commits: source commit messages, future objects, tags, remotes, and alternates are absent. Tree bytes, executable modes, and symlinks are preserved; `export-ignore` does not hide historical files. This isolates the supplied input, not the process: use a disposable filesystem/network sandbox when access to other local directories or the current GitHub PR must be technically prevented. Repositories that require submodules need a separately frozen environment; the tool does not fetch them.

The manifest is evaluator-owned. Each requirement declares `provenance`. A `historical` requirement must quote or faithfully transcribe an original source whose historical availability is established. If original requirements cannot be reconstructed, use only an `evaluator_neutral` review brief, identify its evaluator authorship, and preserve its actual creation time even when later than the historical cutoff. That run is a snapshot smoke with unavailable original requirements, not a reconstructed full historical review. For historical sources, `observed_at` means available at the review cutoff, not when the evaluator downloaded it. A current PR body, a later human comment, an accepted fix, and a merged PR are not substitutes for original requirements or proof of a defect. Omit later context from early steps. Do not put labels, suspected paths, grader answers, or future commit descriptions in the manifest. Every step needs its own full base/head hashes and increasing cutoff.

Start from [manifest.example.json](manifest.example.json), replacing all placeholders. In `environment`, pin the runtime source commit, backend CLI version, Python/toolchain/dependencies, OS or image, checks, per-case budget, memory policy, and other settings that affect the comparison. The script records the instruction and tool digests, actual imported runtime source-package digest, package/Python versions, requested model/effort, resolved task execution, source and synthetic identities, native session/turn IDs, and artifact digests. Generated commands pin the current interpreter and source-package `PYTHONPATH`; preparation and recording reject a changed runtime identity. This checks the supplied execution path, not arbitrary commands run outside it. It does not install dependencies or independently attest that every declared environment setting was applied. Resolve any mismatch before treating a run as comparable. Memory access is empty for the generated task.

Run from the repository with its dependencies installed. Output belongs in a fresh evaluator directory; do not reuse a live service home:

```sh
uv run python scripts/reviewer_eval.py prepare \
  --manifest /evaluation/case.json --repo /evaluation/source \
  --output /evaluation/experiment --arm A --step initial \
  --instructions /evaluation/baseline.md --target codex/YOUR_PINNED_MODEL:high
```

Run the returned `command` in the pinned environment. It selects a separate `NYANPASU_HOME` for each arm and one stable context per arm. If a backend needs nondefault configuration, provision that arm's `config.toml` before execution, keep equivalent backend settings across arms, and preserve the configuration provenance with the experiment. Keep authentication outside recorded artifacts. A/B should differ only in the intended condition. Native backend homes and network access require the same isolation discipline as the execution environment.

Record the step with the returned `record_command` using the same interpreter environment and backend configuration. Recording reads that arm's SQLite database read-only, checks the executed input, then uses the existing native history provider to extract the final answer of the actual final turn. A missing native final answer is an error, not a blank successful review. Optional `--artifact PATH` arguments preserve probes, usage, or other evidence. Provider-native history for the recorded turns is retained privately as `native-history.json`; condition instructions and model identifiers never get copied automatically into the blind packet.

Repeat `prepare` with `--arm B` and the candidate instructions. Record each completed step before preparing the next step of the same arm. Later steps reuse that arm's context/session and receive only context available by their cutoff. Session replacement is recorded as broken continuity and prevents a confirmed timeline score. Keep the frozen first-step output even when a later review revises it. Do not replay a case in the same session across arms or repetitions.

Judges receive only randomized `blind/<sample-id>/` packets and the appropriate historical source trees. The arm mapping, native trace, later fixes/comments, and proposed labels stay with the evaluator. Reports can still reveal their condition through their own wording; inspect for blinding failures. Use a separate trace assessor, or an independently redacted trace, when judging investigation behavior. Do not automatically expose native instructions to the blind judge.

Copy [assessment.example.json](assessment.example.json) to a separate judgments directory for each sample. Leave `status` provisional until a maintainer has reconciled evidence. Every finding needs an exact quote from the native `report.txt` and evidence for its verdict. Extra evaluator artifacts may support the judgment but cannot be counted as model-produced findings. Verdicts are: `confirmed_defect`, `useful_simplification`, `false_positive`, `harmful_suggestion`, or `inconclusive`. Confirmed defects need a stable root-cause identity; `first_present_step` records when the defect already existed. Distinguish a new finding, justified revalidation, and a duplicate. Reconcile related findings and shared-root-cause PRs together; repeated observations are not independent cases.

`expected_defects: null` means coverage is unknown. A non-null map is an independently adjudicated set of defect IDs to evidence sources, including misses; the script refuses different sets for the same step across A/B. An empty map is a positive adjudication, not the default for an unlabeled or merged PR. Accept valid discoveries outside that set and extend labels only through reconciliation. Keep calibration cases and holdout cases separated by root-cause family.

```sh
uv run python scripts/reviewer_eval.py report /evaluation/experiment \
  --judgments /evaluation/judgments
```

The report counts verdicts and occurrences separately, deduplicates confirmed root causes across the timeline, identifies first findings of already-present defects only when the current occurrence is new and all relevant earlier steps have confirmed judgments, preserves unknown late-finding status when those conditions are missing, and reports the coverage denominator only for adjudicated steps. It reports failures, missing judgments, and broken continuity. Historical replay cannot establish counterfactual fix/review rounds saved; report those only from a separate prospective experiment.

Measurements are optional and evidence-backed. Supply `--measurements /evaluation/measurements.json` plus the artifact named by each evidence field:

```json
{
  "first_useful_feedback_at": "2026-10-01T12:00:30Z",
  "first_useful_feedback_evidence": "observations.json",
  "cost_usd": 0.12,
  "cost_evidence": "usage.json",
  "cost_scope": "tree"
}
```

The evaluator must identify the first useful checkpoint and verify the timestamp and usage evidence; the tool cannot decide usefulness or infer a price from arbitrary tokens. Latency starts at runtime task creation, including preparation before the first native turn. Tree cost includes descendants; root-only cost is kept separate and never presented as total cost. Missing values remain unknown. Preserve source usage and pricing assumptions in the named artifact. Repeat material A/B differences and report skipped checks, unavailable hardware, and incomplete labels before making quality claims.

The focused tests use a fake model with the real task runtime, database, workspace handling, and native-history adapter. They establish input isolation, continuity, identity checks, and reporting semantics; they do not establish model review quality. Historical cases and real model results require separate collection and adjudication.
