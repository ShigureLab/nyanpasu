from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import nyanpasu
from nyanpasu.agent import AgentService
from nyanpasu.models import AgentTask
from nyanpasu.transcript.codex import CodexHistorySource
from scripts.reviewer_eval import Assessment, Manifest, git, prepare, prepare_case, read_json, record, report
from tests.test_agent import FakeCodex, _config, fake_backends


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def case(tmp_path):
    assert Path(nyanpasu.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1] / "src")
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--template=", "--initial-branch=main")
    git(repo, "config", "user.name", "Historical author")
    git(repo, "config", "user.email", "author@example.invalid")
    (repo / ".gitattributes").write_text("private.txt export-ignore\n")
    (repo / "private.txt").write_text("historically visible file\n")
    (repo / "app.py").write_text("value = 1\n")
    (repo / "raw.bin").write_bytes(b"raw\r\nbytes\x00\xff")
    (repo / "link").symlink_to("app.py")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("value = 2\n")
    git(repo, "commit", "-am", "first revision")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "future-answer.txt").write_text("FUTURE_EVALUATOR_ANSWER\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "later fix and answer")
    future = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "future-label", future)
    manifest = {
        "schema_version": 1,
        "case_id": "pr-42",
        "repository": "https://github.com/example/project/pull/42",
        "environment": {"python": "3.14", "checks": "pinned project checks"},
        "timeline": [
            {
                "id": "initial",
                "base": base,
                "head": head,
                "cutoff": "2026-10-01T12:00:00Z",
                "requirements": [
                    {
                        "source": "original PR body",
                        "observed_at": "2026-10-01T11:00:00Z",
                        "text": "Preserve delivery semantics.",
                    }
                ],
                "context": [],
            },
            {
                "id": "revision",
                "base": base,
                "head": future,
                "cutoff": "2026-10-02T12:00:00Z",
                "requirements": [
                    {
                        "source": "original PR body",
                        "observed_at": "2026-10-01T11:00:00Z",
                        "text": "Preserve delivery semantics.",
                    }
                ],
                "context": [
                    {
                        "source": "later human request",
                        "observed_at": "2026-10-02T11:00:00Z",
                        "text": "LATER_DISCUSSION_ONLY",
                    }
                ],
            },
        ],
    }
    manifest_path = tmp_path / "case.json"
    manifest_path.write_text(json.dumps(manifest))
    guidance = tmp_path / "guidance.md"
    guidance.write_text("Investigate reachable defects; cite source evidence.\n")
    return {
        "repo": repo,
        "manifest": manifest_path,
        "data": manifest,
        "guidance": guidance,
        "output": tmp_path / "experiment",
    }


def stage(case, arm="A", step="initial"):
    return prepare(case["manifest"], case["repo"], case["output"], arm, step, case["guidance"], "codex/test-model:high")


def test_default_instructions_use_production_builder_and_freeze_actual_guidance(case, monkeypatch):
    import scripts.reviewer_eval as evaluation

    production_builder = evaluation.build_review_instructions
    rendered = []

    def changed_builder(config, pr):
        assert config.dry_run
        assert pr.repo == "evaluation/local"
        assert pr.head_sha == case["data"]["timeline"][0]["head"]
        guidance = production_builder(config, pr) + "\nUPDATED_PRODUCTION_INSTRUCTIONS\n"
        rendered.append(guidance)
        return guidance

    monkeypatch.setattr(evaluation, "build_review_instructions", changed_builder)
    prepared = prepare(case["manifest"], case["repo"], case["output"], "A", "initial", None, "codex/test-model:high")
    task = read_json(Path(prepared["task_path"]))
    guidance = task["developer_instructions"]
    assert len(rendered) == 1 and guidance.startswith(rendered[0])
    assert "read-only; do not write to GitHub" in guidance
    assert "# Local testing during review" in guidance
    assert "Offline evaluation environment" in guidance
    assert (case["output"] / "A/instructions.md").read_text() == guidance


@pytest.mark.anyio
async def test_changed_reviewer_references_reject_resume_and_record(case, tmp_path, monkeypatch):
    import scripts.reviewer_eval as evaluation

    instructions = tmp_path / "reviewer-instructions"
    instructions.mkdir()
    workflow = instructions / "review-workflow.md"
    workflow.write_text("original workflow")
    monkeypatch.setattr(evaluation, "INSTRUCTIONS_DIR", instructions)
    prepared = stage(case)
    workflow.write_text("changed workflow")
    with pytest.raises(ValueError, match="condition changes"):
        stage(case)
    with pytest.raises(ValueError, match="runtime source or interpreter changed"):
        await record(Path(prepared["task_path"]).parent, [], None)


async def execute(prepared, message="Retry can duplicate work.", backend=None):
    task = AgentTask.model_validate(read_json(Path(prepared["task_path"])))
    config = _config(Path(prepared["home"]).parent).model_copy(update={"state_dir": Path(prepared["home"])})

    class Backend(FakeCodex):
        def __init__(self):
            super().__init__()
            self.turns = []

        async def run_turn(self, **kwargs):
            result = await super().run_turn(**kwargs)
            turn_id = f"turn-{len(self.calls)}"
            await kwargs["on_started"](result.thread_id, turn_id)
            self.turns.append(
                {
                    "id": turn_id,
                    "items": [
                        {"id": f"final-{turn_id}", "type": "agentMessage", "phase": "final_answer", "text": message}
                    ],
                }
            )
            return result.model_copy(update={"final_message": message, "turn_id": turn_id})

        async def list_turns(self, thread_id, cursor=None):
            return {"data": self.turns, "nextCursor": None}

    backend = backend or Backend()
    agent = AgentService(config, backends=fake_backends(config, backend))
    try:
        await agent.run_now(task)
    finally:
        await agent.shutdown()
    return backend


def assessment(sample, *, root="retry", first="initial", occurrence="new"):
    return {
        "sample_id": sample,
        "adjudicator": "maintainer-confirmed-test-fixture",
        "status": "confirmed",
        "expected_defects": {root: ["reproduction evidence"]},
        "findings": [
            {
                "finding_id": "F1",
                "verdict": "confirmed_defect",
                "quote": "Retry can duplicate work.",
                "evidence": ["reproduction evidence"],
                "root_cause": root,
                "first_present_step": first,
                "occurrence": occurrence,
            }
        ],
    }


def test_input_stage_contains_only_selected_trees_and_historical_context(case):
    prepared = stage(case)
    root = Path(prepared["task_path"]).parent / "input"
    actual = read_json(Path(prepared["task_path"]))
    source = case["data"]["timeline"]
    assert (root / "private.txt").read_text() == "historically visible file\n"
    assert (root / "raw.bin").read_bytes() == b"raw\r\nbytes\x00\xff"
    assert (root / "link").is_symlink()
    assert not (root / "future-answer.txt").exists()
    assert not (root / ".git/objects/info/alternates").exists()
    assert git(root, "remote") == ""
    assert git(root, "tag") == ""
    assert git(root, "rev-list", "--count", "--all") == "2"
    for revision in (source[0]["base"], source[0]["head"], source[1]["head"]):
        with pytest.raises(subprocess.CalledProcessError):
            git(root, "cat-file", "-e", revision)
    future_blob = git(case["repo"], "rev-parse", "HEAD:future-answer.txt")
    with pytest.raises(subprocess.CalledProcessError):
        git(root, "cat-file", "-e", future_blob)
    assert git(root, "rev-parse", "HEAD^{tree}") == git(case["repo"], "rev-parse", source[0]["head"] + "^{tree}")
    assert "LATER_DISCUSSION_ONLY" not in actual["prompt"]
    assert "FUTURE_EVALUATOR_ANSWER" not in actual["prompt"]
    assert actual["memory"] == {"read_domains": [], "write_domain": None}
    assert stage(case) == prepared


def test_manifest_rejects_future_context_and_evaluator_fields(case):
    data = case["data"]
    data["timeline"][0]["context"] = [{"source": "answer", "observed_at": "2026-10-02T00:00:00Z", "text": "future"}]
    with pytest.raises(ValidationError, match="cutoff"):
        Manifest.model_validate(data)
    data["timeline"][0]["context"] = []
    data["labels"] = {"perfect": True}
    with pytest.raises(ValidationError, match="Extra inputs"):
        Manifest.model_validate(data)


def test_conditions_are_independent_and_timeline_requires_recorded_predecessor(case):
    a, b = stage(case, "A"), stage(case, "B")
    a_task, b_task = (read_json(Path(item["task_path"])) for item in (a, b))
    assert a_task["context_key"] != b_task["context_key"]
    assert a["home"] != b["home"]
    assert a["synthetic"] == b["synthetic"]
    assert a["sample_id"] != b["sample_id"]
    with pytest.raises(ValueError, match="preceding completed"):
        stage(case, "A", "revision")
    case["guidance"].write_text("Different instructions")
    with pytest.raises(ValueError, match="condition changes"):
        stage(case)


@pytest.mark.anyio
async def test_records_real_runtime_identity_and_resumes_only_its_own_branch(case, tmp_path):
    a = stage(case)
    first_dir = Path(a["task_path"]).parent
    with pytest.raises(sqlite3.OperationalError):
        await record(first_dir, [], None)
    backend = await execute(a)
    assert not (backend.calls[-1][0] / "future-answer.txt").exists()
    assert git(backend.calls[-1][0], "rev-parse", "HEAD^{tree}") == a["synthetic"]["head_tree"]
    native = tmp_path / "native.jsonl"
    native.write_text("condition-specific instructions must stay private\n")
    first = await record(first_dir, [native], None, source=CodexHistorySource(backend))
    assert first["task_id"] == a["task_id"]
    assert first["thread_id"]
    assert first["turn_ids"]
    assert first["continuity"] == "fresh"
    assert first["first_useful_feedback_seconds"] is None
    blind = case["output"] / "blind" / first["sample_id"]
    assert not (blind / "native.jsonl").exists()
    assert set(read_json(blind / "sample.json")) == {"sample_id", "source", "step_id", "artifacts"}
    assert (blind / "report.txt").read_text() == "Retry can duplicate work."
    revision = stage(case, "A", "revision")
    assert (Path(revision["task_path"]).parent / "input/future-answer.txt").exists()
    assert "LATER_DISCUSSION_ONLY" in read_json(Path(revision["task_path"]))["prompt"]
    await execute(revision, backend=backend)
    second = await record(Path(revision["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    assert second["continuity"] == "resumed"
    assert second["thread_id"] == first["thread_id"]
    assert len(backend.calls) == 2
    active_workspace = backend.calls[-1][0]
    assert git(active_workspace, "rev-parse", "HEAD") == revision["synthetic"]["head"]
    assert git(active_workspace, "rev-parse", "HEAD^{tree}") == revision["synthetic"]["head_tree"]
    assert (active_workspace / "future-answer.txt").read_text() == "FUTURE_EVALUATOR_ANSWER\n"
    assert git(active_workspace, "tag") == ""
    for original in case["data"]["timeline"]:
        with pytest.raises(subprocess.CalledProcessError):
            git(active_workspace, "cat-file", "-e", original["head"])


@pytest.mark.anyio
async def test_report_requires_actual_quotes_and_separates_unknowns_duplicates_and_late_findings(case, tmp_path):
    initial = stage(case)
    backend = await execute(initial)
    first = await record(Path(initial["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    revision = stage(case, step="revision")
    await execute(revision, backend=backend)
    second = await record(Path(revision["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    judgments = tmp_path / "judgments"
    judgments.mkdir()
    early = assessment(first["sample_id"])
    early["findings"] = []
    (judgments / "one.json").write_text(json.dumps(early))
    later = assessment(second["sample_id"])
    later["findings"].append(
        {
            "finding_id": "F2",
            "verdict": "false_positive",
            "quote": "Retry can duplicate work.",
            "evidence": ["independent counterexample"],
            "occurrence": "duplicate",
        }
    )
    (judgments / "two.json").write_text(json.dumps(later))
    result = report(case["output"], judgments)["arms"]["A"]
    assert result["confirmed_root_causes"] == ["retry"]
    assert result["late_first_findings"] == ["retry"]
    assert result["counts"]["known_defects_found"] == 1
    assert result["counts"]["known_defects_assessed"] == 2
    assert result["counts"]["false_positive"] == result["counts"]["occurrence_duplicate"] == 1
    assert result["cost_usd_by_scope"]["tree"] == {"observed_runs": 0, "total": None}
    assert result["first_useful_feedback_seconds"] == []
    later["findings"][0]["quote"] = "Invented finding absent from the run"
    (judgments / "two.json").write_text(json.dumps(later))
    with pytest.raises(ValueError, match="quote is absent"):
        report(case["output"], judgments)


@pytest.mark.anyio
async def test_measurements_need_evidence_and_use_actual_runtime_window(case, tmp_path):
    prepared = stage(case)
    backend = await execute(prepared)
    step_dir = Path(prepared["task_path"]).parent
    with sqlite3.connect(Path(prepared["home"]) / "state.sqlite3") as conn:
        start = conn.execute("SELECT created_at FROM task_runs WHERE task_id=?", (prepared["task_id"],)).fetchone()[0]
        end = conn.execute("SELECT updated_at FROM task_runs WHERE task_id=?", (prepared["task_id"],)).fetchone()[0]
    metrics = tmp_path / "measurements.json"
    payload = {
        "first_useful_feedback_at": datetime.fromtimestamp((start + end) / 2, UTC).isoformat(),
        "first_useful_feedback_evidence": "observations.json",
        "cost_usd": 0.25,
        "cost_evidence": "observations.json",
        "cost_scope": "tree",
    }
    metrics.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="supplied artifact"):
        await record(step_dir, [], metrics, source=CodexHistorySource(backend))
    evidence = tmp_path / "observations.json"
    evidence.write_text(json.dumps(payload))
    result = await record(step_dir, [evidence], metrics, source=CodexHistorySource(backend))
    assert result["first_useful_feedback_seconds"] == pytest.approx((end - start) / 2, abs=1e-6)
    summary = report(case["output"], None)["arms"]["A"]
    assert summary["counts"]["unconfirmed_runs"] == 1
    assert summary["cost_usd_by_scope"]["tree"] == {"observed_runs": 1, "total": 0.25}
    assert Assessment(sample_id=result["sample_id"], adjudicator="unreviewed suggestion").status == "provisional"


@pytest.mark.anyio
async def test_record_rejects_wrong_native_turn_and_changed_runtime_input(case):
    prepared = stage(case)
    backend = await execute(prepared)
    directory = Path(prepared["task_path"]).parent
    backend.turns[0]["id"] = "unrelated-turn"
    with pytest.raises(ValueError, match="native history"):
        await record(directory, [], None, source=CodexHistorySource(backend))
    backend.turns[0]["id"] = "turn-1"
    with sqlite3.connect(Path(prepared["home"]) / "state.sqlite3") as conn:
        conn.execute("UPDATE task_runs SET task_json=json_set(task_json,'$.prompt','different input')")
    with pytest.raises(ValueError, match="frozen input: prompt"):
        await record(directory, [], None, source=CodexHistorySource(backend))


@pytest.mark.anyio
async def test_broken_timeline_is_recorded_but_cannot_be_scored_as_continuous(case, tmp_path):
    first = stage(case)
    backend = await execute(first)
    await record(Path(first["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    second = stage(case, step="revision")
    with sqlite3.connect(Path(second["home"]) / "state.sqlite3") as conn:
        conn.execute("UPDATE agent_contexts SET thread_id='replacement-thread'")
    backend = await execute(second, backend=backend)
    result = await record(Path(second["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    assert result["continuity"] == "broken"
    judgments = tmp_path / "judgments"
    judgments.mkdir()
    (judgments / "one.json").write_text(json.dumps(assessment(result["sample_id"])))
    with pytest.raises(ValueError, match="discontinuous"):
        report(case["output"], judgments)


def test_neutral_brief_preserves_actual_authorship_and_runtime_changes_are_rejected(case, monkeypatch):
    data = case["data"]
    requirement = data["timeline"][0]["requirements"][0]
    requirement.update(
        provenance="evaluator_neutral", observed_at="2026-10-21T00:00:00Z", source="evaluator-authored neutral brief"
    )
    case["manifest"].write_text(json.dumps(data))
    prepared = stage(case)
    prompt = read_json(Path(prepared["task_path"]))["prompt"]
    assert "evaluator_neutral" in prompt and "2026-10-21" in prompt
    import scripts.reviewer_eval as evaluation

    previous = evaluation.runtime_identity()
    monkeypatch.setattr(evaluation, "runtime_identity", lambda: {**previous, "source_sha256": "changed-runtime"})
    with pytest.raises(ValueError, match="condition changes"):
        stage(case)


@pytest.mark.anyio
async def test_evaluator_attachment_cannot_be_counted_as_a_model_finding(case, tmp_path):
    prepared = stage(case)
    backend = await execute(prepared, message="No supported findings.")
    attachment = tmp_path / "evaluator-source.txt"
    attachment.write_text("Retry can duplicate work.")
    result = await record(Path(prepared["task_path"]).parent, [attachment], None, source=CodexHistorySource(backend))
    judgments = tmp_path / "judgments"
    judgments.mkdir()
    proposed = assessment(result["sample_id"])
    proposed["findings"][0]["artifact"] = attachment.name
    path = judgments / "one.json"
    path.write_text(json.dumps(proposed))
    with pytest.raises(ValidationError, match="report.txt"):
        report(case["output"], judgments)
    proposed["findings"][0]["artifact"] = "report.txt"
    path.write_text(json.dumps(proposed))
    with pytest.raises(ValueError, match="quote is absent"):
        report(case["output"], judgments)


@pytest.mark.anyio
@pytest.mark.parametrize("occurrence", ["new", "revalidation"])
async def test_missing_earlier_judgment_is_not_counted_as_a_missed_finding(case, tmp_path, occurrence):
    first = stage(case)
    backend = await execute(first)
    await record(Path(first["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    second = stage(case, step="revision")
    await execute(second, backend=backend)
    result = await record(Path(second["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    judgments = tmp_path / "judgments"
    judgments.mkdir()
    (judgments / "one.json").write_text(json.dumps(assessment(result["sample_id"], occurrence=occurrence)))
    observed = report(case["output"], judgments)["arms"]["A"]
    assert observed["late_first_findings"] == []
    assert observed["late_status_unknown"] == ["retry"]
    assert observed["counts"]["unconfirmed_runs"] == 1


REAL_PR = Path(__file__).resolve().parents[1] / "evals/reviewer/real-pr"


@pytest.fixture
def real_pr_cases(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    cases = []
    responses = {}
    inputs = {}
    for index, names in enumerate((("src/app.py", "src/consumer.py"), ("config/check.py",))):
        case_id = f"fixture-{index}"
        specs = []
        for file_index, name in enumerate(names):
            content = f"# scoped input\nvalue = {index + file_index}\n".encode()
            spec = {
                "path": name,
                "commit": "a" * 39 + str(index),
                "sha256": hashlib.sha256(content).hexdigest(),
                "git_blob_sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
            }
            if file_index:
                spec["repository"] = "example/dependency"
            specs.append(spec)
            inputs[case_id, name] = content
            repo = spec.get("repository", "example/project")
            responses[f"https://raw.githubusercontent.com/{repo}/{spec['commit']}/{name}"] = content
        cases.append(
            {
                "case_id": case_id,
                "repository": "example/project",
                "url": f"https://github.com/example/project/pull/{index + 1}",
                "evaluation": {
                    "kind": "source-snapshot",
                    "environment": {"python": "3.10", "scope": "supplied implementation"},
                    "cutoff": "2026-10-01T12:00:00Z",
                    "requirements": [
                        {
                            "source": "evaluator brief",
                            "provenance": "evaluator_neutral",
                            "observed_at": "2026-10-25T00:00:00Z",
                            "text": "Preserve the documented consumer contract.",
                        }
                    ],
                    "inputs": specs,
                    "expected": {
                        "kind": "retention",
                        "root_cause": f"{case_id}-root",
                        "behavior": "Preserve the consumer-required conversion.",
                        "source": "source evidence",
                        "limits": "one bounded contract",
                    },
                },
            }
        )
    cases.append({"case_id": "unprepared-case", "repository": "example/project", "evaluation": None})
    cases_path = dataset / "cases.json"
    cases_path.write_text(json.dumps({"cases": cases}))
    requests = []

    def download(url, timeout):
        assert timeout == 30
        requests.append(url)
        return io.BytesIO(responses[url])

    monkeypatch.setattr(urllib.request, "urlopen", download)
    return {
        "path": cases_path,
        "cases": cases[:2],
        "cache": tmp_path / "sources",
        "inputs": inputs,
        "requests": requests,
    }


def test_unified_cases_stage_only_selected_inputs_without_grading_material(tmp_path, real_pr_cases):
    cache = real_pr_cases["cache"]
    cache.mkdir()
    (cache / "evaluator-answer.txt").write_text("never copy this into reviewer input\n")
    fetched = 0
    for index, case in enumerate(real_pr_cases["cases"]):
        evaluation = case["evaluation"]
        specs = evaluation["inputs"]
        fetched += len(specs)
        for arm in ("A", "B"):
            result = prepare_case(
                real_pr_cases["path"],
                case["case_id"],
                tmp_path / f"run-{index}",
                arm,
                None,
                "codex/test-model:high",
                cache,
            )
            assert result["case_id"] == case["case_id"]
            task_path = Path(result["task_path"])
            staged = task_path.parent / "input"
            assert git(staged, "ls-files").splitlines() == sorted(spec["path"] for spec in specs)
            for spec in specs:
                assert (staged / spec["path"]).read_bytes() == real_pr_cases["inputs"][case["case_id"], spec["path"]]
            assert read_json(tmp_path / f"run-{index}/evaluation.json")["expected"] == evaluation["expected"]
            assert git(staged, "rev-list", "--all", "--count") == "2"
            assert git(staged, "remote") == ""
            prompt = read_json(task_path)["prompt"]
            assert evaluation["requirements"][0]["text"] in prompt
            assert "not the original PR diff" in prompt
            assert "evaluator_neutral" in prompt
            for private in [
                case["case_id"],
                case["url"],
                *[spec["commit"] for spec in specs],
                evaluation["expected"]["root_cause"],
                evaluation["expected"]["behavior"],
            ]:
                assert private not in task_path.read_text()
            # Only the selected input is fetched. Arm B reuses the verified cache.
            assert len(real_pr_cases["requests"]) == fetched


@pytest.mark.parametrize("fault", ["download", "cache", "blob-id"])
def test_unified_case_rejects_changed_download_or_cached_source(tmp_path, real_pr_cases, monkeypatch, fault):
    case = real_pr_cases["cases"][0]
    spec = case["evaluation"]["inputs"][0]
    cached = real_pr_cases["cache"] / spec["sha256"] / Path(spec["path"]).name
    if fault == "download":
        monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"wrong source\n"))
    elif fault == "blob-id":
        metadata = read_json(real_pr_cases["path"])
        next(c for c in metadata["cases"] if c["case_id"] == case["case_id"])["evaluation"]["inputs"][0][
            "git_blob_sha"
        ] = "0" * 40
        real_pr_cases["path"].write_text(json.dumps(metadata))
    else:
        cached.parent.mkdir(parents=True)
        cached.write_text("corrupted cache\n")
    with pytest.raises(ValueError, match="source hash mismatch"):
        prepare_case(
            real_pr_cases["path"],
            case["case_id"],
            tmp_path / "runs",
            "A",
            None,
            "codex/test-model:high",
            real_pr_cases["cache"],
        )
    assert not (tmp_path / "runs").exists()
    if fault != "cache":
        assert not cached.exists()


def test_source_cache_must_be_outside_checkout(tmp_path):
    with pytest.raises(ValueError, match="outside the source checkout"):
        prepare_case(
            REAL_PR / "cases.json",
            "paddlepaddle-paddlesot-110",
            tmp_path / "run",
            "A",
            None,
            "codex/test-model:high",
            REAL_PR.parent / "cache",
        )


def test_unified_case_refuses_mutable_upstream_ref(tmp_path, real_pr_cases):
    case = real_pr_cases["cases"][0]
    metadata = read_json(real_pr_cases["path"])
    next(c for c in metadata["cases"] if c["case_id"] == case["case_id"])["evaluation"]["inputs"][0]["commit"] = "main"
    real_pr_cases["path"].write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="full commit SHA"):
        prepare_case(
            real_pr_cases["path"],
            case["case_id"],
            tmp_path / "runs",
            "A",
            None,
            "codex/test-model:high",
            real_pr_cases["cache"],
        )
    assert real_pr_cases["requests"] == []


@pytest.mark.parametrize("case_id, message", [("unprepared-case", "not runnable"), ("does-not-exist", "unknown case")])
def test_unified_case_rejects_unprepared_or_unknown_case(tmp_path, real_pr_cases, case_id, message):
    with pytest.raises(ValueError, match=message):
        prepare_case(
            real_pr_cases["path"],
            case_id,
            tmp_path / "runs",
            "A",
            None,
            "codex/test-model:high",
            real_pr_cases["cache"],
        )
    assert real_pr_cases["requests"] == []
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("paths", [[], ["../outside.py"], ["same.py", "same.py"]])
def test_multifile_case_rejects_invalid_paths_before_fetch(tmp_path, real_pr_cases, paths):
    data = read_json(real_pr_cases["path"])
    case = data["cases"][0]
    spec = case["evaluation"]["inputs"][0]
    case["evaluation"]["inputs"] = [{**spec, "path": path} for path in paths]
    real_pr_cases["path"].write_text(json.dumps(data))
    with pytest.raises(ValueError, match="source paths"):
        prepare_case(
            real_pr_cases["path"],
            case["case_id"],
            tmp_path / "runs",
            "A",
            None,
            "codex/test-model:high",
            real_pr_cases["cache"],
        )
    assert real_pr_cases["requests"] == []
    assert not (tmp_path / "runs").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["provisional", "confirmed"])
async def test_retention_criterion_scores_frozen_native_output_without_a_fake_defect(tmp_path, real_pr_cases, status):
    case = real_pr_cases["cases"][0]
    output = tmp_path / "run"
    prepared = prepare_case(
        real_pr_cases["path"], case["case_id"], output, "A", None, "codex/test-model:high", real_pr_cases["cache"]
    )
    message = "Keep the conversion because the consumer requires int32."
    backend = await execute(prepared, message=message)
    result = await record(Path(prepared["task_path"]).parent, [], None, source=CodexHistorySource(backend))
    judgments = tmp_path / "judgments"
    judgments.mkdir()
    item = {
        "sample_id": result["sample_id"],
        "adjudicator": "test fixture",
        "status": status,
        "criteria": [
            {
                "root_cause": case["evaluation"]["expected"]["root_cause"],
                "outcome": "pass",
                "quote": message,
                "evidence": ["pinned consumer contract"],
            }
        ],
    }
    path = judgments / "one.json"
    path.write_text(json.dumps(item))
    observed = report(output, judgments)["arms"]["A"]
    assert observed["counts"].get("criterion_pass", 0) == (status == "confirmed")
    assert observed["counts"].get("confirmed_defect", 0) == 0
    assert observed["counts"].get("known_defects_assessed", 0) == 0
    if status == "confirmed":
        assert observed["criteria"][0]["kind"] == "retention"
    item["criteria"][0]["root_cause"] = "a-different-target"
    path.write_text(json.dumps(item))
    with pytest.raises(ValueError, match="frozen case target"):
        report(output, judgments)
    item["criteria"][0]["root_cause"] = case["evaluation"]["expected"]["root_cause"]
    item["criteria"][0]["quote"] = "invented model output"
    path.write_text(json.dumps(item))
    with pytest.raises(ValueError, match="quote is absent"):
        report(output, judgments)
