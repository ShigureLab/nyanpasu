from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

import nyanpasu.agent as agent_module
from nyanpasu.models import TaskStatus
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_hybrid_memory import ALICE, config_for, make_agent
from tests.test_memory_pipeline import MemoryModel
from tests.test_rolling_memory import CONTEXT, SequentialTurns, complete_memory, continuing_task


def extraction_id(name):
    return f"memory:{hashlib.sha256(name.encode()).hexdigest()}"


def producer_clock(monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(
        agent_module,
        "time",
        SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, time_ns=time.time_ns),
    )
    return clock


def batched_agent(tmp_path, model, count=3, **config):
    return make_agent(
        config_for(tmp_path, consolidate=True, idle_after_seconds=300, **config),
        codex=SequentialTurns(new_session_id="shared-session"),
        cheap=model,
        source=MemorySessionSource(
            [turn(f"turn-{index}", tool(f"proof-{index}", f"VERIFIED_LESSON_{index}")) for index in range(1, count + 1)]
            + [turn("unbound", tool("foreign", "PRIVATE_UNBOUND_MATERIAL"))]
        ),
    )


@pytest.mark.anyio
async def test_idle_window_batches_consecutive_tasks_once_and_resets_on_new_activity(tmp_path, monkeypatch):
    clock = producer_clock(monkeypatch)
    model = MemoryModel()
    agent = batched_agent(tmp_path, model)
    try:
        await agent.run_now(continuing_task("first"))
        first_finished = agent.store.task_run("first").updated_at
        clock[0] = first_finished + 299
        assert await agent.sweep_memory() == []
        await agent.run_now(continuing_task("second"))
        clock[0] = first_finished + 300
        assert await agent.sweep_memory() == []
        await agent.run_now(continuing_task("third"))
        assert model.events == []
        assert not any(item.kind.startswith("memory_") for item in agent.store.recent_tasks())

        clock[0] = agent.store.task_run("third").updated_at + 300
        jobs = await agent.sweep_memory()
        assert [job.task_id for job in jobs] == [extraction_id("third")]
        await complete_memory(agent, "third")

        assert [kind for kind, _ in model.events] == ["extraction", "summary"]
        material = model.events[0][1]
        assert {item["source_task_id"] for item in material["evidence"]} == {"first", "second", "third"}
        assert "PRIVATE_UNBOUND_MATERIAL" not in json.dumps(material)
        saved = agent.memory.source_state(ALICE, CONTEXT)
        assert saved is not None and set(saved.task_digests) == {"first", "second", "third"}
        assert {ref for ref in saved.sources if ref.startswith("task:")} == {"task:first", "task:second", "task:third"}
        for index in range(1, 4):
            assert saved.body.count(f"VERIFIED_LESSON_{index}") == 1

        clock[0] += 3600
        assert await agent.sweep_memory() == []
        assert len(model.events) == 2
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_restart_discovers_unscheduled_completed_contributions(tmp_path, monkeypatch):
    clock = producer_clock(monkeypatch)
    model = MemoryModel()
    agent = batched_agent(tmp_path, model, count=2)
    for name in ("first", "second"):
        await agent.run_now(continuing_task(name))
    finished = agent.store.task_run("second").updated_at
    assert agent.store.unfinished_tasks() == [] and model.events == []
    await agent.shutdown()

    recovered = batched_agent(tmp_path, model, count=2)
    try:
        clock[0] = finished + 301
        await recovered.startup()
        await complete_memory(recovered, "second")
        assert [kind for kind, _ in model.events] == ["extraction", "summary"]
        assert await recovered.sweep_memory() == []
    finally:
        await recovered.shutdown()


@pytest.mark.anyio
async def test_queued_work_keeps_context_ineligible_for_production(tmp_path, monkeypatch):
    clock = producer_clock(monkeypatch)
    model = MemoryModel()
    agent = batched_agent(tmp_path, model)
    try:
        await agent.run_now(continuing_task("first"))
        second = agent._admit(continuing_task("second"))
        agent.store.record_task(second)
        clock[0] = agent.store.task_run("first").updated_at + 1000
        assert await agent.sweep_memory() == []
        assert model.events == []
        agent.store.mark_task_failed("second", "backend failed before starting")
        clock[0] = agent.store.task_run("second").updated_at + 299
        assert await agent.sweep_memory() == []
        clock[0] += 1
        assert len(await agent.sweep_memory()) == 1
        await complete_memory(agent, "first")
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_failed_batch_does_not_advance_receipts_or_loop_on_every_sweep(tmp_path, monkeypatch):
    clock = producer_clock(monkeypatch)
    model = MemoryModel()
    model.fail_at.add(1)
    agent = batched_agent(tmp_path, model, count=2)
    try:
        for name in ("first", "second"):
            await agent.run_now(continuing_task(name))
        clock[0] = agent.store.task_run("second").updated_at + 300
        await agent.sweep_memory()
        failed = await agent.wait_for_memory(extraction_id("second"))
        assert failed.status is TaskStatus.FAILED
        assert agent.memory.source_state(ALICE, CONTEXT) is None
        assert await agent.sweep_memory() == []
        assert len(model.events) == 1

        retry = await agent.rebuild_memory("second")
        assert (await agent.wait_for_memory(retry.task_id)).status is TaskStatus.COMPLETED
        assert model.events[1][1]["evidence"] == model.events[0][1]["evidence"]
        saved = agent.memory.source_state(ALICE, CONTEXT)
        assert saved is not None and set(saved.task_digests) == {"first", "second"}
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_empty_account_advances_receipts_without_a_summary_job(tmp_path, monkeypatch):
    clock = producer_clock(monkeypatch)
    model = MemoryModel()
    model.responses[1] = json.dumps({"title": "No durable lessons", "body": "", "topics": []})
    agent = batched_agent(tmp_path, model, count=2)
    try:
        for name in ("first", "second"):
            await agent.run_now(continuing_task(name))
        clock[0] = agent.store.task_run("second").updated_at + 300
        await agent.sweep_memory()
        result = await agent.wait_for_memory(extraction_id("second"))
        assert result.status is TaskStatus.COMPLETED and result.kind == "memory_extraction"
        assert [kind for kind, _ in model.events] == ["extraction"]
        saved = agent.memory.source_state(ALICE, CONTEXT)
        assert saved is not None and set(saved.task_digests) == {"first", "second"}
        assert agent.memory.list_sources(ALICE) == agent.memory.list_summaries(ALICE) == []
        assert await agent.sweep_memory() == []
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_periodic_producer_runs_when_idle_window_expires_without_another_task(tmp_path):
    model = MemoryModel()
    config = config_for(tmp_path, consolidate=True, idle_after_seconds=0.05, sweep_interval_seconds=0.01)
    agent = make_agent(
        config,
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "Verified lesson"))]),
    )
    try:
        await agent.run_now(continuing_task("first"))
        assert model.events == []

        async def await_admission():
            while agent.store.task_status(extraction_id("first")) is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(await_admission(), 5)
        await complete_memory(agent, "first")
        assert [kind for kind, _ in model.events] == ["extraction", "summary"]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_folded_events_are_extracted_through_the_executed_task_not_as_turnless_sources(tmp_path, monkeypatch):
    clock = producer_clock(monkeypatch)
    model = MemoryModel()
    agent = batched_agent(tmp_path, model, count=2)

    async def prepare(source, coalesced, context):
        return source.model_copy(update={"prompt": "\n".join([source.prompt, *(item.prompt for item in coalesced)])})

    agent.add_task_preparer("test", prepare)
    first = agent._admit(
        continuing_task("first").model_copy(update={"coalesce_key": "events", "metadata": {"plugin_id": "test"}})
    )
    folded = first.model_copy(update={"task_id": "folded", "prompt": "FOLDED_REQUEST"})
    agent.store.record_task(first)
    assert agent.store.enqueue_task(folded, coalesce_since=0) == (True, "first")
    try:
        await agent._run_task(first)
        await agent.run_now(continuing_task("later"))
        assert agent.store.task_turns("folded") == []
        clock[0] = agent.store.task_run("later").updated_at + 300
        await agent.sweep_memory()
        await complete_memory(agent, "later")
        saved = agent.memory.source_state(ALICE, CONTEXT)
        assert saved is not None and set(saved.task_digests) == {"first", "later"}
        assert "FOLDED_REQUEST" in saved.body
        assert [kind for kind, _ in model.events] == ["extraction", "summary"]
    finally:
        await agent.shutdown()
