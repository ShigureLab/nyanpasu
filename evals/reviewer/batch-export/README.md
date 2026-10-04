# Batch export evaluation

This is a small synthetic repository, inspired by review experience rather than copied from a project. It tests investigation of a change across callers, persisted state, and a receiver boundary. It supplies both a simplification opportunity and behavior that must survive simplification. It does not establish general review quality.

## Reviewer input

Give the reviewer only `requirements.md`, `base/`, and `head/`, copied to a disposable input directory outside this repository. Do not expose this document, the parent evaluation documents, or `evaluator/`. For an independent design condition, initially give that child only `requirements.md` and `base/`; freeze its output before revealing the head.

For a guidance comparison, start fresh sessions with the same model, effort, tools, budget, and input. Supply the old or new guidance separately, with neutral run labels. Disable shared/native memory and do not give later sessions earlier results. Preserve submitted inputs and guidance, actual model settings, transcripts including tool calls, elapsed time, costs when available, and unavailable checks. Have a maintainer grade anonymized outputs and their evidence. Keep evaluator feedback until the run ends.

The input has no named bug category or request to investigate a particular mechanism. A reviewer can discover questions by tracing the ordinary entry point and existing contracts. Do not provide the probes below as reviewer tasks or reward a report for reproducing their names.

## Fixture checks

These commands require only Python 3.11+ and Git. Run from the Nyanpasu repository root. Both submitted baseline suites should pass:

```sh
python -B -m unittest discover -s evals/reviewer/batch-export/base -p checks.py -v
python -B -m unittest discover -s evals/reviewer/batch-export/head -p checks.py -v
```

The evaluator's independent behavioral witnesses execute the head's real exporter, checkpoint file, and SQLite receiver. The unmodified head intentionally exits 1: the repeated-record scenario raises `ValueError`, the rejected-delivery retry loses its first batch, and the lost-acknowledgement scenario passes.

```sh
python -B evals/reviewer/batch-export/evaluator/probe.py evals/reviewer/batch-export/head -v
```

Apply the small alternative in a disposable copy. It removes the content ledger and changes checkpoint ordering, while keeping the receiver request identity and acknowledgement behavior. The submitted suite and all three witnesses should pass:

```sh
case_dir="$PWD/evals/reviewer/batch-export"
trial_dir="$(mktemp -d)"
cp -R "$case_dir/head/." "$trial_dir/"
git -C "$trial_dir" apply "$case_dir/evaluator/simpler.patch"
python -B -m unittest discover -s "$trial_dir" -p checks.py -v
python -B "$case_dir/evaluator/probe.py" "$trial_dir" -v
```

Then apply one plausible defect to the alternative: reject a repeated receiver request instead of acknowledging its committed delivery. The submitted suite still passes, but the lost-acknowledgement witness must now fail with `ValueError`. This demonstrates why the remaining receiver boundary matters:

```sh
git -C "$trial_dir" apply "$case_dir/evaluator/reject-retry.patch"
python -B -m unittest discover -s "$trial_dir" -p checks.py -v
python -B "$case_dir/evaluator/probe.py" "$trial_dir" -v
```

Record actual output and exit status; import, patch, or environment failures are inconclusive. These checks validate the fixture and its behavioral witnesses. They are not model runs or evidence that revised reviewer guidance improves review quality. The injected connection failures exercise the actual local persistence paths; they do not validate a real network transport, power-loss durability, concurrent exporters, or changing input files.

## Running a model

Use the existing Nyanpasu task/session interface with an isolated evaluation home and explicit execution target. Run within the reviewer's execution environment, including its container when deployed that way; do not substitute host binaries for container paths. `uv run nyanpasu run-task task.json --target <backend/model:effort>` runs a real native session; the task should use a fresh context, memory disabled, the staged input workspace, and the chosen guidance. Do not copy live credentials into the input directory or start/restart the production service.

`run-task` alone does not install the GitHub reviewer plugin's preparers and controls. A generic task with the review guidance can establish a behavior smoke result, but cannot establish that the complete review/publication lifecycle ran. Record exactly which instructions and controls were supplied. Existing runtime tests and these fixture checks are separate evidence from either kind of model evaluation.
