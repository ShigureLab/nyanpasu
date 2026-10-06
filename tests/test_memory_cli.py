from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from nyanpasu import __main__ as cli
from nyanpasu.models import TaskRunResult, TaskStatus
from tests.session_source import MemorySessionSource
from tests.test_hybrid_memory import PUBLIC, config_for, make_agent, task
from tests.test_memory import legacy_sources
from tests.test_memory_pipeline import MemoryModel, publish_source, publish_summary


@pytest.mark.parametrize("fails", [False, True])
def test_rebuild_command_retries_summary_and_reports_publication_result(tmp_path, monkeypatch, fails):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    if fails:
        model.fail_at.add(1)
    agent = make_agent(config, cheap=model)
    source = agent._admit(task("source"))
    agent.store.record_task(source)
    agent.store.mark_task_running(source.task_id, None)
    agent.store.mark_task_done(
        TaskRunResult(
            task_id=source.task_id,
            status=TaskStatus.COMPLETED,
            thread_id=None,
            turn_id=None,
            final_message="verified result",
        )
    )
    checkpoint = publish_source(agent.memory, PUBLIC, source.task_id)
    extraction = agent._memory_task(source.task_id, "public", task_id="memory:extraction", kind="memory_extraction")
    failed = agent._memory_followup(extraction)
    assert failed is not None
    agent.store.record_task(failed)
    agent.store.mark_task_running(failed.task_id, None)
    agent.store.mark_task_failed(failed.task_id, "previous summary failed")
    shutdown = AsyncMock(wraps=agent.shutdown)
    monkeypatch.setattr(agent, "shutdown", shutdown)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "AgentService", lambda _: agent)

    result = CliRunner().invoke(cli.app, ["memory-rebuild", source.task_id])

    assert result.exit_code == (1 if fails else 0), result.output
    status = json.loads(result.stdout)
    assert status["status"] == ("failed" if fails else "completed")
    assert status["kind"] == "memory_consolidation"
    assert status["task_id"] != failed.task_id
    assert agent.store.task_status(failed.task_id) == "failed"
    assert agent.memory.source_state(PUBLIC, source.context_key, source.context_generation) == checkpoint
    assert [kind for kind, _ in model.events] == ["summary"]
    summary = agent.memory.list_summaries(PUBLIC)
    if fails:
        assert summary == []
    else:
        assert summary[0].sources == (checkpoint.id,)
    shutdown.assert_awaited_once()


@pytest.mark.parametrize("fails", [False, True])
def test_rebuild_all_binds_legacy_contexts_and_reuses_sources_without_native_history(tmp_path, monkeypatch, fails):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    if fails:
        model.fail_at.add(1)
    native = MemorySessionSource()
    agent = make_agent(config, cheap=model, source=native)
    originals = []
    for name, context in (("first", "pr-257"), ("followup", "pr-257"), ("other", "pr-258")):
        source = agent._admit(task(name).model_copy(update={"context_key": context}))
        agent.store.record_task(source)
        agent.store.mark_task_running(name, None)
        agent.store.mark_task_done(
            TaskRunResult(
                task_id=name,
                status=TaskStatus.COMPLETED,
                thread_id=None,
                turn_id=None,
                final_message="verified outcome",
            )
        )
        originals.append(publish_source(agent.memory, PUBLIC, name, body=f"Retained evidence for {name}"))
    manifest_path, old_navigation = legacy_sources(agent.memory)
    shutdown = AsyncMock(wraps=agent.shutdown)
    monkeypatch.setattr(agent, "shutdown", shutdown)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "AgentService", lambda _: agent)

    result = CliRunner().invoke(cli.app, ["memory-rebuild", "--all"])

    assert result.exit_code == (1 if fails else 0), result.output
    results = json.loads(result.stdout)
    assert len(results) == 2
    assert {result["kind"] for result in results} == {"memory_consolidation"}
    assert sum(result["status"] == "failed" for result in results) == int(fails)
    assert [kind for kind, _ in model.events] == ["summary", "summary"]
    inputs = {material["context_key"]: material for _, material in model.events}
    assert set(inputs) == {"pr-257", "pr-258"}
    accounts = agent.memory.list_sources(PUBLIC)
    assert len(accounts) == 2
    by_context = {source.context_key: source for source in accounts}
    assert set(by_context) == {"pr-257", "pr-258"}
    merged = by_context["pr-257"]
    separate = by_context["pr-258"]
    assert [item["id"] for item in inputs["pr-257"]["source_accounts"]] == [merged.id]
    assert [item["id"] for item in inputs["pr-258"]["source_accounts"]] == [separate.id]
    assert inputs["pr-257"]["source_accounts"][0]["body"] == merged.body
    assert inputs["pr-258"]["source_accounts"][0]["body"] == separate.body
    assert originals[0].body in merged.body and originals[1].body in merged.body
    assert originals[2].body not in merged.body
    assert merged.sources == tuple(sorted({*originals[0].sources, *originals[1].sources}))
    assert separate.body == originals[2].body and separate.sources == originals[2].sources
    assert agent.memory.read(PUBLIC, originals[0].id) == agent.memory.read(PUBLIC, originals[1].id) == merged
    assert native.calls == []
    for original in originals:
        source = agent.memory.read(PUBLIC, original.id)
        assert original.body in source.body and set(original.sources).issubset(source.sources)
        assert source.context_key == ("pr-258" if original.task_id == "other" else "pr-257")
        assert source.context_generation == 1
        assert source.source_order > 0
    background = [item for item in agent.store.recent_tasks() if item.kind == "memory_consolidation"]
    assert len(background) == 2
    assert {result["task_id"] for result in results} == {item.task_id for item in background}
    assert sum(item.status is TaskStatus.FAILED for item in background) == int(fails)
    summaries = agent.memory.list_summaries(PUBLIC)
    assert len(summaries) == (1 if fails else 2)
    assert not any(summary.stale for summary in summaries)
    assert "navigation" not in json.loads(manifest_path.read_text())
    assert not old_navigation.exists()
    shutdown.assert_awaited_once()


def test_rebuild_all_empty_corpus_returns_an_empty_success_result(tmp_path, monkeypatch):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    agent = make_agent(config, cheap=model)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "AgentService", lambda _: agent)

    result = CliRunner().invoke(cli.app, ["memory-rebuild", "--all"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert model.events == []
    assert agent.store.recent_tasks() == []


@pytest.mark.parametrize("source_state", ["withdrawn", "partial"])
def test_rebuild_all_uses_complete_publications_even_when_empty_or_a_newer_draft_exists(
    tmp_path, monkeypatch, source_state
):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    native = MemorySessionSource()
    agent = make_agent(config, cheap=model, source=native)
    root = agent._admit(task("source"))
    agent.store.record_task(root)
    agent.store.mark_task_running(root.task_id, None)
    agent.store.mark_task_done(
        TaskRunResult(
            task_id=root.task_id,
            status=TaskStatus.COMPLETED,
            thread_id=None,
            turn_id=None,
            final_message="completed source task",
        )
    )
    original = publish_source(agent.memory, PUBLIC, root.task_id, body="Published verified evidence")
    if source_state == "withdrawn":
        publish_summary(agent.memory, PUBLIC, f"Historical conclusion [evidence](memory:{original.id})")
    latest = publish_source(
        agent.memory,
        PUBLIC,
        root.task_id,
        body="" if source_state == "withdrawn" else "UNFINISHED NEW EXTRACTION",
        complete=source_state == "withdrawn",
        input_digest="changed-input",
        expected_revision=original.revision,
    )
    assert len(agent.memory.list_sources(PUBLIC, include_empty=True)) == 1
    assert len(agent.memory.list_sources(PUBLIC)) == (0 if source_state == "withdrawn" else 1)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "AgentService", lambda _: agent)

    result = CliRunner().invoke(cli.app, ["memory-rebuild", "--all"])

    assert result.exit_code == 0, result.output
    results = json.loads(result.stdout)
    assert len(results) == 1 and results[0]["kind"] == "memory_consolidation"
    assert results[0]["status"] == "completed"
    assert native.calls == []
    assert agent.memory.source_state(PUBLIC, root.context_key, root.context_generation) == latest
    summary = agent.memory.snapshot_context(PUBLIC, root.context_key, root.context_generation).summary
    assert summary is not None and not summary.stale
    if source_state == "withdrawn":
        assert model.events == []
        assert summary.body == "" and summary.sources == () and summary.source_revisions == {}
    else:
        assert [kind for kind, _ in model.events] == ["summary"]
        accounts = model.events[0][1]["source_accounts"]
        assert [(item["id"], item["body"]) for item in accounts] == [(original.id, original.body)]
        assert summary.source_revisions == {original.id: original.content_revision}
        assert agent.memory.read(PUBLIC, original.id).revision == original.revision
