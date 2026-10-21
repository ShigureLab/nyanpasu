"""Prepare isolated real-PR inputs and report evidence-backed reviewer evaluations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import anyio
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

import nyanpasu
from nyanpasu.backends import Backends
from nyanpasu.config import NyanpasuConfig, load_config
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentTask, TaskAction, WorkspaceRef
from nyanpasu.targets import ExecutionOverride

if TYPE_CHECKING:
    from nyanpasu.transcript.history import SessionSource

Slug = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Revision = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Nonempty = Annotated[str, Field(min_length=1)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextItem(FrozenModel):
    source: Nonempty
    provenance: Literal["historical", "evaluator_neutral"] = "historical"
    observed_at: AwareDatetime
    text: Nonempty


class Step(FrozenModel):
    id: Slug
    base: Revision
    head: Revision
    cutoff: AwareDatetime
    requirements: list[ContextItem] = Field(min_length=1)
    context: list[ContextItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def historical_context(self) -> Step:
        if any(item.provenance != "historical" for item in self.context):
            raise ValueError("timeline context must be historical")
        if any(
            item.observed_at > self.cutoff
            for item in [*self.requirements, *self.context]
            if item.provenance == "historical"
        ):
            raise ValueError("historical requirements and context must have been observed by this step's cutoff")
        return self


class Manifest(FrozenModel):
    schema_version: Literal[1]
    case_id: Slug
    repository: Nonempty
    environment: dict[str, Nonempty] = Field(min_length=1)
    timeline: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_steps(self) -> Manifest:
        if len({step.id for step in self.timeline}) != len(self.timeline):
            raise ValueError("timeline step IDs must be unique")
        if any(a.cutoff >= b.cutoff for a, b in zip(self.timeline, self.timeline[1:], strict=False)):
            raise ValueError("timeline cutoffs must increase strictly")
        return self


class Measurement(FrozenModel):
    first_useful_feedback_at: AwareDatetime | None = None
    first_useful_feedback_evidence: str | None = None
    cost_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    cost_evidence: str | None = None
    cost_scope: Literal["root", "tree"] | None = None

    @model_validator(mode="after")
    def sourced_values(self) -> Measurement:
        if (self.first_useful_feedback_at is None) != (self.first_useful_feedback_evidence is None):
            raise ValueError("first useful feedback needs both a timestamp and an artifact name")
        if self.cost_usd is not None and (not self.cost_evidence or self.cost_scope is None):
            raise ValueError("cost needs an artifact name and an explicit root/tree scope")
        if self.cost_usd is None and (self.cost_evidence is not None or self.cost_scope is not None):
            raise ValueError("cost evidence without a cost")
        return self


class FindingJudgment(FrozenModel):
    finding_id: Nonempty
    verdict: Literal[
        "confirmed_defect", "useful_simplification", "false_positive", "harmful_suggestion", "inconclusive"
    ]
    artifact: Literal["report.txt"] = "report.txt"
    quote: Nonempty
    evidence: list[Nonempty] = Field(min_length=1)
    root_cause: str | None = None
    first_present_step: str | None = None
    occurrence: Literal["new", "revalidation", "duplicate"] = "new"

    @model_validator(mode="after")
    def defect_identity(self) -> FindingJudgment:
        if self.verdict == "confirmed_defect" and not self.root_cause:
            raise ValueError("confirmed defects require a root-cause identity")
        return self


class Assessment(FrozenModel):
    sample_id: Nonempty
    adjudicator: Nonempty
    status: Literal["provisional", "confirmed"] = "provisional"
    expected_defects: dict[str, list[Nonempty]] | None = None
    findings: list[FindingJudgment] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_findings(self) -> Assessment:
        if len({item.finding_id for item in self.findings}) != len(self.findings):
            raise ValueError("finding IDs must be unique within one assessment")
        if self.expected_defects is not None and any(not sources for sources in self.expected_defects.values()):
            raise ValueError("each expected defect needs an evidence source")
        return self


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def freeze(path: Path, content: bytes) -> None:
    """Publish an immutable file without exposing a partially written JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f"immutable evaluation artifact differs: {path}") from None
    finally:
        temporary.unlink()


def git(path: Path, *args: str, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, input=input_text, env=env, text=True, capture_output=True, check=True
    ).stdout.strip()


def runtime_identity() -> dict[str, Any]:
    package = Path(nyanpasu.__file__).resolve().parent
    sources = {str(path.relative_to(package)): sha256(path.read_bytes()) for path in sorted(package.rglob("*.py"))}
    return {
        "source_path": str(package),
        "source_sha256": sha256(json_bytes(sources)),
        "package_version": version("nyanpasu"),
        "python": sys.version,
        "interpreter": sys.executable,
    }


def stage_repository(source: Path, destination: Path, step: Step) -> dict[str, str]:
    """Copy only two trees, never source commits, remotes, alternate stores or future refs."""
    destination.mkdir()
    git(
        destination,
        "init",
        "--template=",
        "--initial-branch=review",
        f"--object-format={git(source, 'rev-parse', '--show-object-format')}",
    )
    (destination / ".git/info").mkdir(exist_ok=True)
    (destination / ".git/info/attributes").write_text("* -text -filter -working-tree-encoding -ident\n")
    manager = WorktreeManager(NyanpasuConfig(state_dir=destination.parent / "unused-runtime"))
    workspace = WorkspaceRef(key="evaluation-source", local_path=source)
    commits = {}
    parent = []
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Reviewer evaluation",
        "GIT_AUTHOR_EMAIL": "reviewer-eval@localhost",
        "GIT_COMMITTER_NAME": "Reviewer evaluation",
        "GIT_COMMITTER_EMAIL": "reviewer-eval@localhost",
        "GIT_AUTHOR_DATE": step.cutoff.isoformat(),
        "GIT_COMMITTER_DATE": step.cutoff.isoformat(),
    }
    for name, revision in (("base", step.base), ("head", step.head)):
        if git(source, "rev-parse", "--verify", f"{revision}^{{commit}}") != revision:
            raise ValueError("the manifest must pin full commit identities")
        tree, objects = manager._materialize_tree(workspace, revision)
        manager._import_objects(workspace, destination, objects)
        commit = git(destination, "commit-tree", tree, *parent, input_text=f"Evaluation {name} snapshot\n", env=env)
        git(destination, "update-ref", f"refs/eval/{name}", commit)
        commits[name] = commit
        commits[f"{name}_tree"] = tree
        parent = ["-p", commit]
    git(destination, "-c", "core.hooksPath=/dev/null", "reset", "--hard", commits["head"])
    return commits


def prepare(
    manifest_path: Path, source: Path, output: Path, arm: str, step_id: str, instructions: Path, target: str
) -> dict[str, Any]:
    manifest = Manifest.model_validate(read_json(manifest_path))
    source, output = source.resolve(), output.resolve()
    if output.is_relative_to(source):
        raise ValueError("evaluation output must be outside the source repository")
    if arm not in {"A", "B"}:
        raise ValueError("arm must be A or B")
    indices = {step.id: index for index, step in enumerate(manifest.timeline)}
    if step_id not in indices:
        raise ValueError("unknown timeline step")
    index = indices[step_id]
    step = manifest.timeline[index]
    override = ExecutionOverride.model_validate(target)
    if override.backend is None or override.model is None or override.reasoning is None:
        raise ValueError("pin backend, model and reasoning in the execution target")
    manifest_data = manifest.model_dump(mode="json")
    freeze(output / "manifest.json", json_bytes(manifest_data))
    guidance = instructions.read_bytes()
    branch = output / arm
    identity_path = branch / "identity.json"
    identity: dict[str, Any]
    expected: dict[str, Any] = {
        "target": override.model_dump(),
        "instructions_sha256": sha256(guidance),
        "tool_sha256": sha256(Path(__file__).read_bytes()),
        "runtime": runtime_identity(),
    }
    if identity_path.exists():
        identity = read_json(identity_path)
        if any(identity[key] != value for key, value in expected.items()):
            raise ValueError("use a new experiment directory when a condition changes")
    else:
        if index:
            raise ValueError("start each arm at its first timeline step")
        identity = {**expected, "context_key": f"reviewer-eval:{uuid.uuid4()}", "arm": arm}
        freeze(identity_path, json_bytes(identity))
        freeze(branch / "instructions.md", guidance)
    step_dir = branch / "steps" / f"{index:03d}"
    if index:
        prior = branch / "steps" / f"{index - 1:03d}" / "record.json"
        if not prior.exists() or read_json(prior)["status"] != "completed":
            raise ValueError("record the preceding completed step before preparing the next revision")
    if (step_dir / "prepared.json").exists():
        return read_json(step_dir / "prepared.json")
    step_dir.parent.mkdir(parents=True, exist_ok=True)
    home = branch / "home"
    home.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=step_dir.parent, prefix="staging-") as temporary:
        staging = Path(temporary)
        commits = stage_repository(source, staging / "input", step)
        visible = {
            "repository": manifest.repository,
            "source_base": step.base,
            "source_head": step.head,
            "cutoff": step.cutoff.isoformat(),
            "requirements": [item.model_dump(mode="json") for item in step.requirements],
            "context": [item.model_dump(mode="json") for item in step.context],
            "environment": manifest.environment,
        }
        prompt = (
            "Review the pinned change using the supplied requirements and historical context. "
            "Preserve their declared provenance: an evaluator_neutral brief is not a recovered original requirement. "
            f"The synthetic base is {commits['base']}; the synthetic head is {commits['head']}. "
            "Their trees match the source revisions below. "
            + (
                "Continue this arm's saved review, reconcile its previous findings and investigate affected behavior. "
                if index
                else "This is the first review of this change in a fresh evaluation session. "
            )
            + "Use only the supplied historical evidence and staged repository; do not fetch current PR data, "
            "other experiment conditions or evaluator material. Return supported findings and evidence gaps. "
            "This generic offline task does not provide the GitHub reviewer plugin's admission or publication tools. "
            "Do not publish externally.\n\nHistorical input:\n" + json.dumps(visible, ensure_ascii=False, indent=2)
        )
        task = AgentTask(
            task_id=str(uuid.uuid4()),
            action=TaskAction.RUN,
            context_key=identity["context_key"],
            prompt=prompt,
            execution_override=override,
            developer_instructions=guidance.decode(),
            workspace=WorkspaceRef(
                key=identity["context_key"], local_path=step_dir / "input", revision=commits["head"]
            ),
        )
        task_data = task.model_dump(mode="json")
        environment = ["env", f"NYANPASU_HOME={home}", f"PYTHONPATH={Path(nyanpasu.__file__).resolve().parent.parent}"]
        prepared = {
            "schema_version": 1,
            "sample_id": str(uuid.uuid4()),
            "case_id": manifest.case_id,
            "step_id": step.id,
            "step_index": index,
            "arm": arm,
            "manifest_sha256": sha256(json_bytes(manifest_data)),
            "tool_sha256": sha256(Path(__file__).read_bytes()),
            "runtime": identity["runtime"],
            "source": {"repository": manifest.repository, "base": step.base, "head": step.head},
            "synthetic": commits,
            "task_sha256": sha256(json_bytes(task_data)),
            "task_id": task.task_id,
            "home": str(home),
            "task_path": str(step_dir / "task.json"),
            "command": shlex.join(
                [*environment, sys.executable, "-m", "nyanpasu", "run-task", str(step_dir / "task.json")]
            ),
            "record_command": shlex.join(
                [*environment, sys.executable, str(Path(__file__).resolve()), "record", str(step_dir)]
            ),
            "isolation": "staged input only; filesystem and network are not sandboxed",
            "lifecycle": "generic run-task; not the complete GitHub reviewer plugin lifecycle",
        }
        (staging / "task.json").write_bytes(json_bytes(task_data))
        (staging / "prepared.json").write_bytes(json_bytes(prepared))
        staging.rename(step_dir)
    return prepared


def runtime_record(prepared: dict[str, Any], expected: AgentTask) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    db = Path(prepared["home"]) / "state.sqlite3"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM task_runs WHERE task_id=?", (expected.task_id,)).fetchone()
        if row is None or row["status"] not in {"completed", "failed", "cancelled"}:
            raise ValueError("record requires an actual terminal task in this arm's isolated runtime database")
        actual = AgentTask.model_validate_json(row["task_json"])
        for name in (
            "task_id",
            "context_key",
            "prompt",
            "developer_instructions",
            "workspace",
            "execution_override",
            "memory",
        ):
            if getattr(actual, name) != getattr(expected, name):
                raise ValueError(f"runtime task differs from the frozen input: {name}")
        turns = [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM task_turns WHERE task_id=? ORDER BY started_at,turn_id", (expected.task_id,)
            )
        ]
        if row["status"] == "completed" and (not row["thread_id"] or not turns):
            raise ValueError("a completed evaluation must identify its native session and turn")
        return dict(row), turns


async def record(
    step_dir: Path, artifact_paths: list[Path], measurements_path: Path | None, *, source: SessionSource | None = None
) -> dict[str, Any]:
    step_dir = step_dir.resolve()
    prepared = read_json(step_dir / "prepared.json")
    if runtime_identity() != prepared["runtime"]:
        raise ValueError("runtime source or interpreter changed after preparation")
    task_bytes = (step_dir / "task.json").read_bytes()
    if sha256(task_bytes) != prepared["task_sha256"]:
        raise ValueError("the prepared task changed")
    expected = AgentTask.model_validate_json(task_bytes)
    row, turns = runtime_record(prepared, expected)
    measurements = Measurement.model_validate(read_json(measurements_path)) if measurements_path else Measurement()
    contents = {}
    if row["thread_id"]:
        backends = None
        if source is None:
            config = load_config()
            if config.state_dir != Path(prepared["home"]):
                raise ValueError("set NYANPASU_HOME to this arm's prepared home when recording native history")
            backends = Backends(config)
            source = backends.source(row["backend"])
        try:
            history = await source.read_session(row["thread_id"])
        finally:
            if backends is not None:
                await backends.close()
        turn_ids = {turn["turn_id"] for turn in turns}
        selected = tuple(turn for turn in history.turns if turn.id in turn_ids)
        final_items = [
            item
            for turn in selected
            if turn.id == row["turn_id"]
            for item in turn.items
            if item.presentation.kind == "message" and item.presentation.phase == "final_answer"
        ]
        if history.metadata.id != row["thread_id"] or (row["status"] == "completed" and not final_items):
            raise ValueError("native history does not contain the recorded task's final answer")
        contents["report.txt"] = "\n\n".join(
            block.text for item in final_items for block in item.presentation.blocks
        ).encode()
        contents["native-history.json"] = json_bytes(
            history.model_copy(update={"turns": selected}).model_dump(mode="json")
        )
    else:
        contents["report.txt"] = b""
    for path in artifact_paths:
        if path.name in contents:
            raise ValueError("artifact names must be distinct; report.txt and native-history.json are reserved")
        contents[path.name] = path.read_bytes()
    for name in (measurements.cost_evidence, measurements.first_useful_feedback_evidence):
        if name is not None and name not in contents:
            raise ValueError("measurement evidence must name a supplied artifact")
    start = row["created_at"]
    feedback = measurements.first_useful_feedback_at
    if feedback is not None and not start <= feedback.timestamp() <= row["updated_at"]:
        raise ValueError("first useful feedback must fall within this recorded run")
    continuity = "fresh"
    if prepared["step_index"]:
        previous = read_json(step_dir.parent / f"{prepared['step_index'] - 1:03d}" / "record.json")
        continuity = "resumed" if row["thread_id"] and row["thread_id"] == previous["thread_id"] else "broken"
    artifacts = {name: {"sha256": sha256(content), "bytes": len(content)} for name, content in contents.items()}
    result = {
        "sample_id": prepared["sample_id"],
        "task_id": row["task_id"],
        "runtime": prepared["runtime"],
        "status": row["status"],
        "thread_id": row["thread_id"],
        "turn_ids": [turn["turn_id"] for turn in turns],
        "execution": json.loads(row["task_json"]).get("execution"),
        "continuity": continuity,
        "started_at": start,
        "native_started_at": min((turn["started_at"] for turn in turns), default=None),
        "finished_at": row["updated_at"],
        "elapsed_seconds": row["updated_at"] - start,
        "first_useful_feedback_seconds": feedback.timestamp() - start if feedback is not None else None,
        "measurements": measurements.model_dump(mode="json"),
        "artifacts": artifacts,
    }
    for name, content in contents.items():
        freeze(step_dir / "artifacts" / name, content)
    freeze(step_dir / "runtime.json", json_bytes({"task": row, "turns": turns}))
    freeze(step_dir / "record.json", json_bytes(result))
    blind = step_dir.parents[2] / "blind" / prepared["sample_id"]
    freeze(
        blind / "sample.json",
        json_bytes(
            {
                "sample_id": prepared["sample_id"],
                "source": prepared["source"],
                "step_id": prepared["step_id"],
                "artifacts": {"report.txt": artifacts["report.txt"]},
            }
        ),
    )
    # Native transcripts contain condition instructions and model identities.
    # Keep them with the private run record, not in the blind judging packet.
    freeze(blind / "report.txt", contents["report.txt"])
    return result


def report(output: Path, judgments: Path | None) -> dict[str, Any]:
    manifest = Manifest.model_validate(read_json(output / "manifest.json"))
    steps = {step.id: index for index, step in enumerate(manifest.timeline)}
    assessments = {}
    if judgments is not None:
        for path in sorted(judgments.glob("*.json")):
            item = Assessment.model_validate(read_json(path))
            if item.sample_id in assessments:
                raise ValueError("provide one reconciled assessment per anonymous sample")
            assessments[item.sample_id] = item
    arms = {}
    seen_samples = set()
    expected_by_step = {}
    for arm in ("A", "B"):
        counts: Counter[str] = Counter()
        roots = set()
        late_roots = set()
        unknown_late_roots = set()
        confirmed_steps = set()
        timings = []
        costs = {"root": [], "tree": []}
        results = []
        for path in sorted((output / arm / "steps").glob("*/record.json")):
            result = read_json(path)
            prepared = read_json(path.parent / "prepared.json")
            seen_samples.add(result["sample_id"])
            counts["recorded_runs"] += 1
            counts[f"status_{result['status']}"] += 1
            counts[f"continuity_{result['continuity']}"] += 1
            if result["first_useful_feedback_seconds"] is not None:
                timings.append(result["first_useful_feedback_seconds"])
            measure = result["measurements"]
            if measure["cost_usd"] is not None:
                costs[measure["cost_scope"]].append(measure["cost_usd"])
            assessment = assessments.get(result["sample_id"])
            if assessment is not None:
                for finding in assessment.findings:
                    artifact = result["artifacts"].get(finding.artifact)
                    evidence_path = path.parent / "artifacts" / finding.artifact
                    if artifact is None or sha256(evidence_path.read_bytes()) != artifact["sha256"]:
                        raise ValueError("judgment must cite an unchanged recorded artifact")
                    if finding.quote not in evidence_path.read_text():
                        raise ValueError("judgment quote is absent from the recorded artifact")
                    if finding.first_present_step is not None and (
                        finding.first_present_step not in steps
                        or steps[finding.first_present_step] > prepared["step_index"]
                    ):
                        raise ValueError("a defect's first-present step must be known and no later than this review")
            if assessment is None or assessment.status != "confirmed":
                counts["unconfirmed_runs"] += 1
            else:
                if result["status"] != "completed" or result["continuity"] == "broken":
                    raise ValueError("failed or discontinuous runs cannot receive a confirmed quality assessment")
                counts["confirmed_assessments"] += 1
                known = assessment.expected_defects
                if known is not None:
                    if prepared["step_id"] in expected_by_step and expected_by_step[prepared["step_id"]] != set(known):
                        raise ValueError("reconcile the known-defect set across conditions before reporting recall")
                    expected_by_step[prepared["step_id"]] = set(known)
                    counts["known_defects_assessed"] += len(known)
                    found = {
                        finding.root_cause for finding in assessment.findings if finding.verdict == "confirmed_defect"
                    }
                    counts["known_defects_found"] += len(set(known) & found)
                for finding in assessment.findings:
                    counts[finding.verdict] += 1
                    counts[f"occurrence_{finding.occurrence}"] += 1
                    if finding.verdict == "confirmed_defect":
                        assert finding.root_cause is not None  # Validated by FindingJudgment.
                        if finding.root_cause not in roots and finding.first_present_step is not None:
                            prior_steps = set(range(steps[finding.first_present_step], prepared["step_index"]))
                            if prior_steps:
                                if finding.occurrence == "new" and prior_steps <= confirmed_steps:
                                    late_roots.add(finding.root_cause)
                                else:
                                    unknown_late_roots.add(finding.root_cause)
                        roots.add(finding.root_cause)
                confirmed_steps.add(prepared["step_index"])
            results.append(
                {"sample_id": result["sample_id"], "step_id": prepared["step_id"], "status": result["status"]}
            )
        if results:
            arms[arm] = {
                "counts": dict(counts),
                "confirmed_root_causes": sorted(roots),
                "late_first_findings": sorted(late_roots),
                "late_status_unknown": sorted(unknown_late_roots),
                "first_useful_feedback_seconds": timings,
                "cost_usd_by_scope": {
                    scope: {"observed_runs": len(values), "total": sum(values) if values else None}
                    for scope, values in costs.items()
                },
                "runs": results,
            }
    if set(assessments) - seen_samples:
        raise ValueError("assessment names an unknown recorded sample")
    return {
        "case_id": manifest.case_id,
        "arms": arms,
        "limitations": [
            "Known-defect counts cover adjudicated labels only; an unlabelled or merged PR is not proven clean.",
            "Historical replay measures review behavior, not counterfactual fix/review rounds saved.",
            "Generic run-task does not validate the full GitHub reviewer plugin lifecycle.",
            "Missing cost and feedback observations remain unknown, not zero; confirm quoted evidence separately.",
            "Anonymous packets omit condition metadata; report/transcript content may still reveal a condition.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="Materialize one timeline step for one condition")
    for name in ("manifest", "repo", "output", "instructions"):
        prepare_parser.add_argument(f"--{name}", type=Path, required=True)
    prepare_parser.add_argument("--arm", choices=("A", "B"), required=True)
    prepare_parser.add_argument("--step", required=True)
    prepare_parser.add_argument("--target", required=True, help="backend/model[:reasoning]")
    record_parser = commands.add_parser("record", help="Freeze a real runtime result and anonymized artifacts")
    record_parser.add_argument("step_dir", type=Path)
    record_parser.add_argument("--artifact", type=Path, action="append", default=[])
    record_parser.add_argument("--measurements", type=Path)
    report_parser = commands.add_parser("report", help="Summarize reconciled, evidence-backed judgments")
    report_parser.add_argument("output", type=Path)
    report_parser.add_argument("--judgments", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.manifest, args.repo, args.output, args.arm, args.step, args.instructions, args.target)
        elif args.command == "record":
            result = anyio.run(record, args.step_dir, args.artifact, args.measurements)
        else:
            result = report(args.output, args.judgments)
    except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
