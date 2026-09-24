from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Small, directed experiments. No mutation score or model-quality claim is implied.
EXPERIMENTS = [
    (
        "child-reserves-root-slot",
        "src/nyanpasu/agent.py",
        "async with self._context_execution(task):\n                return await self._run_execution(task)",
        "async with self._context_execution(task), self._semaphore:\n                return await self._run_execution(task)",
        "tests/test_subtask_runtime.py::test_children_run_while_parent_runs_and_waits_without_admitting_another_root",
        1,
    ),
    (
        "wait-not-consumed",
        "src/nyanpasu/store.py",
        "SET wait_for=NULL,status='queued'",
        "SET status='queued'",
        "tests/test_subtask_state.py::test_wait_survives_restart_and_observes_child_finishing_on_either_side",
        1,
    ),
    (
        "reference-starts-from-head",
        "packages/nyanpasu-github-reviewer/src/nyanpasu_github_reviewer/reference.py",
        "source = bases[0]",
        "source = head",
        "tests/test_review_reference.py::test_independent_child_uses_common_base_without_author_history",
        1,
    ),
    (
        "rename-private-export-helper",
        "src/nyanpasu/git_ops.py",
        "_snapshot(",
        "_export_base_tree(",
        "tests/test_review_reference.py",
        0,
    ),
]


def main():
    parser = argparse.ArgumentParser(description="Run directed subtask experiments in a disposable HEAD export")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    archive = subprocess.check_output(["git", "archive", "HEAD"], cwd=repo)
    results = []
    with tempfile.TemporaryDirectory(prefix="nyanpasu-mutations-") as directory:
        copy = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
            tree.extractall(copy, filter="data")
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(map(str, [copy, copy / "src", *sorted((copy / "packages").glob("*/src"))])),
        }
        tests = list(dict.fromkeys(case[4] for case in EXPERIMENTS))
        baseline = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *tests],
            cwd=copy,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        (output / "baseline.log").write_text(baseline.stdout + baseline.stderr)
        if baseline.returncode:
            raise SystemExit("Baseline failed; experiments are inconclusive")
        for name, file, old, new, test, expected in EXPERIMENTS:
            path = copy / file
            original = path.read_text()
            if old not in original:
                raise SystemExit(f"Experiment no longer applies: {name}")
            try:
                changed = (
                    original.replace("self._snapshot(", "self._export_base_tree(").replace(
                        "def _snapshot(", "def _export_base_tree("
                    )
                    if name == "rename-private-export-helper"
                    else original.replace(old, new)
                )
                path.write_text(changed)
                run = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "--tb=short", test],
                    cwd=copy,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                (output / f"{name}.log").write_text(run.stdout + run.stderr)
                result = {
                    "name": name,
                    "exit_code": run.returncode,
                    "expected": expected,
                    "test": test,
                    "summary": run.stdout.splitlines()[-1] if run.stdout else "",
                }
                results.append(result)
                print(json.dumps(result), flush=True)
                if run.returncode != expected or "ERROR collecting" in run.stdout:
                    raise SystemExit(f"Unexpected outcome: {name}; inspect the retained log")
            finally:
                path.write_text(original)
        (output / "results.json").write_text(json.dumps({"head": head, "experiments": results}, indent=2))


if __name__ == "__main__":
    main()
