from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from nyanpasu.memory_consolidation import BLOCK_LIMIT, input_digest
from nyanpasu.models import TaskStatus
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_agent import FakeCodex
from tests.test_hybrid_memory import ALICE, config_for, make_agent, task
from tests.test_memory import legacy_sources
from tests.test_memory_pipeline import MemoryModel

CONTEXT = "continuing-conversation"


class SequentialTurns(FakeCodex):
    """Bind each root task to its own turn in the same native session."""

    async def run_turn(self, *, on_started=None, **kwargs):
        turn_id = f"turn-{len(self.calls) + 1}"

        async def started(thread_id, _turn_id):
            if on_started is not None:
                await on_started(thread_id, turn_id)

        result = await super().run_turn(on_started=started, **kwargs)
        return result.model_copy(update={"turn_id": turn_id})


def continuing_task(name):
    return task(name, memory=ALICE).model_copy(update={"context_key": CONTEXT})


async def complete_memory(agent, task_id):
    identity = f"memory:{hashlib.sha256(task_id.encode()).hexdigest()}"
    result = await asyncio.wait_for(agent.wait_for_memory(identity), 5)
    assert result.status is TaskStatus.COMPLETED, result
    return result


@pytest.mark.anyio
async def test_successive_tasks_update_one_account_with_only_their_bound_evidence(tmp_path):
    native = MemorySessionSource(
        [
            turn("unbound", tool("foreign", "UNBOUND_PRIVATE_MATERIAL")),
            turn("turn-1", tool("first-proof", "FIRST_VERIFIED_LESSON")),
            turn("turn-2", tool("second-proof", "SECOND_VERIFIED_LESSON")),
        ]
    )
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        codex=SequentialTurns(new_session_id="shared-session"),
        cheap=model,
        source=native,
    )
    try:
        await agent.run_now(continuing_task("first"))
        await complete_memory(agent, "first")
        initial = agent.memory.list_sources(ALICE)[0]
        initial_summary = agent.memory.list_summaries(ALICE)[0]

        await agent.run_now(continuing_task("second"))
        await complete_memory(agent, "second")

        sources = agent.memory.list_sources(ALICE)
        summaries = agent.memory.list_summaries(ALICE)
        assert len(sources) == len(summaries) == 1
        current, summary = sources[0], summaries[0]
        assert current.id == initial.id and current.revision != initial.revision
        assert summary.id == initial_summary.id and not summary.stale
        assert summary.sources == (current.id,)
        assert current.task_id == "second"
        assert current.body.count("FIRST_VERIFIED_LESSON") == 1
        assert current.body.count("SECOND_VERIFIED_LESSON") == 1
        assert set(current.sources) == {
            "task:first",
            "task:second",
            "codex:shared-session:turn-1:first-proof",
            "codex:shared-session:turn-2:second-proof",
        }
        checkpoint = agent.memory.source_state(ALICE, CONTEXT)
        assert checkpoint is not None and set(checkpoint.task_digests) == {"first", "second"}

        extraction = [material for kind, material in model.events if kind == "extraction"]
        assert [material["source_task_id"] for material in extraction] == ["first", "second"]
        assert extraction[1]["previous_account"]["body"] == initial.body
        first_evidence, second_evidence = [json.dumps(material["evidence"]) for material in extraction]
        assert "FIRST_VERIFIED_LESSON" in first_evidence and "SECOND_VERIFIED_LESSON" not in first_evidence
        assert "SECOND_VERIFIED_LESSON" in second_evidence and "FIRST_VERIFIED_LESSON" not in second_evidence
        assert "UNBOUND_PRIVATE_MATERIAL" not in json.dumps(model.events)
        assert agent.store.task_turns("first")[0]["turn_id"] == "turn-1"
        assert agent.store.task_turns("second")[0]["turn_id"] == "turn-2"
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("failed_chunk", [1, 2])
@pytest.mark.parametrize("has_previous_account", [False, True])
async def test_new_task_recovers_earlier_failed_extraction_in_order(tmp_path, failed_chunk, has_previous_account):
    earlier_turn = 2 if has_previous_account else 1
    records = [tool(f"proof-{index}", f"EARLIER_PIECE_{index}:" + "x" * BLOCK_LIMIT * 3) for index in range(4)]
    native = MemorySessionSource(
        ([turn("turn-1", tool("seed-proof", "PREEXISTING_VERIFIED_LESSON"))] if has_previous_account else [])
        + [
            turn(f"turn-{earlier_turn}", *records),
            turn(f"turn-{earlier_turn + 1}", tool("later-proof", "LATER_VERIFIED_LESSON")),
        ]
    )
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        codex=SequentialTurns(new_session_id="shared-session"),
        cheap=model,
        source=native,
    )
    try:
        previous = None
        if has_previous_account:
            await agent.run_now(continuing_task("seed"))
            await complete_memory(agent, "seed")
            previous = agent.memory.list_sources(ALICE)[0]
        failure_number = len(model.events) + failed_chunk
        model.fail_at.add(failure_number)
        await agent.run_now(continuing_task("earlier"))
        failed_id = f"memory:{hashlib.sha256(b'earlier').hexdigest()}"
        failed = await asyncio.wait_for(agent.wait_for_memory(failed_id), 5)
        assert failed.status is TaskStatus.FAILED
        # A failed update leaves the previous publication visible throughout recovery.
        assert agent.memory.list_sources(ALICE) == ([previous] if previous is not None else [])
        failed_evidence = model.events[-1][1]["evidence"]
        committed_evidence = model.events[-2][1]["evidence"] if failed_chunk == 2 else None
        event_count = len(model.events)

        await agent.run_now(continuing_task("later"))
        await complete_memory(agent, "later")

        assert len(agent.memory.list_sources(ALICE)) == 1
        current = agent.memory.list_sources(ALICE)[0]
        assert current.task_id == "later"
        assert current.body.count("LATER_VERIFIED_LESSON") == 1
        for index in range(4):
            assert current.body.count(f"EARLIER_PIECE_{index}") == 1
        expected_tasks = {"earlier", "later"}
        if previous is not None:
            expected_tasks.add("seed")
            assert current.id == previous.id
            assert current.body.count("PREEXISTING_VERIFIED_LESSON") == 1
        checkpoint = agent.memory.source_state(ALICE, CONTEXT)
        assert checkpoint is not None and checkpoint.complete
        assert set(checkpoint.task_digests) == expected_tasks
        assert {reference for reference in current.sources if reference.startswith("task:")} == {
            f"task:{name}" for name in expected_tasks
        }
        assert set(current.sources) >= {f"codex:shared-session:turn-{earlier_turn}:proof-{index}" for index in range(4)}
        recovered = [material for kind, material in model.events[event_count:] if kind == "extraction"]
        assert recovered[0]["source_task_id"] == "earlier"
        assert recovered[0]["evidence"] == failed_evidence
        assert [material["source_task_id"] for material in recovered] == ["earlier"] * (len(recovered) - 1) + ["later"]
        if committed_evidence is not None:
            assert all(material["evidence"] != committed_evidence for material in recovered)
        assert len(agent.memory.list_summaries(ALICE)) == 1
        assert agent.memory.list_summaries(ALICE)[0].sources == (current.id,)
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_retrying_absorbed_old_task_uses_receipts_without_reading_native_history(tmp_path):
    native = MemorySessionSource(
        [
            turn("turn-1", tool("first-proof", "FIRST_VERIFIED_LESSON")),
            turn("turn-2", tool("second-proof", "LATEST_VERIFIED_CORRECTION")),
        ]
    )
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        codex=SequentialTurns(new_session_id="shared-session"),
        cheap=model,
        source=native,
    )
    try:
        for name in ("first", "second"):
            await agent.run_now(continuing_task(name))
            await complete_memory(agent, name)
        published = agent.memory.list_sources(ALICE)
        summary = agent.memory.list_summaries(ALICE)
        native_reads, model_calls = len(native.calls), len(model.events)

        retry = await agent.rebuild_memory("first")
        assert (await asyncio.wait_for(agent.wait_for_memory(retry.task_id), 5)).status is TaskStatus.COMPLETED

        assert len(native.calls) == native_reads
        assert len(model.events) == model_calls
        assert agent.memory.list_sources(ALICE) == published
        assert agent.memory.list_summaries(ALICE) == summary
        assert published[0].task_id == "second"
        assert "LATEST_VERIFIED_CORRECTION" in published[0].body
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_format", [False, True], ids=["rolling", "legacy"])
async def test_changed_task_draft_revokes_old_receipt_and_rebuilds_new_evidence(tmp_path, legacy_format):
    native = MemorySessionSource([turn("turn-1", tool("old-proof", "PREVIOUSLY_PUBLISHED_LESSON"))])
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        codex=SequentialTurns(new_session_id="shared-session"),
        cheap=model,
        source=native,
    )
    try:
        await agent.run_now(continuing_task("source"))
        await complete_memory(agent, "source")
        published = agent.memory.list_sources(ALICE)[0]
        original = agent.memory.source_state(ALICE, CONTEXT)
        assert original is not None

        # A revised native result has begun extraction, but only its first chunk
        # was checkpointed. Its old publication must no longer count as a receipt.
        native.turns = [
            turn(
                "turn-1",
                tool("updated-proof", "NEW_PARTIAL_LESSON:" + "x" * BLOCK_LIMIT * 5),
                tool("final-proof", "NEW_FINAL_VERIFIED_RESULT"),
            )
        ]
        source = agent.store.task_request("source")
        chunks = await agent._source_chunks(source)
        assert len(chunks) > 1
        digest = input_digest(chunks)
        assert digest != original.input_digest
        partial_body = published.body + "\n" + "\n".join(item["text"][:64] for item in chunks[0])
        partial = agent.memory.checkpoint_source(
            ALICE,
            source.task_id,
            context_key=CONTEXT,
            context_generation=source.context_generation,
            source_order=original.source_order,
            input_digest=digest,
            cursor=1,
            complete=False,
            title=published.title,
            body=partial_body,
            topics=published.topics,
            sources=[item["reference"] for item in chunks[0]],
            expected_revision=original.revision,
        )
        assert agent.memory.read(ALICE, published.id).body == published.body
        if legacy_format:
            legacy_sources(agent.memory, ALICE)

        event_count = len(model.events)
        model.fail_at.add(event_count + 1)
        failed_retry = await agent.rebuild_memory("source")
        assert failed_retry.kind == "memory_extraction"
        failed = await asyncio.wait_for(agent.wait_for_memory(failed_retry.task_id), 5)
        assert failed.status is TaskStatus.FAILED
        assert agent.memory.read(ALICE, published.id).body == published.body

        retry = await agent.rebuild_memory("source")
        assert retry.kind == "memory_extraction"
        assert (await asyncio.wait_for(agent.wait_for_memory(retry.task_id), 5)).status is TaskStatus.COMPLETED

        current = agent.memory.list_sources(ALICE)
        assert len(current) == 1
        assert agent.memory.read(ALICE, published.id).id == current[0].id
        assert current[0].body.count("PREVIOUSLY_PUBLISHED_LESSON") == 1
        assert current[0].body.count("NEW_PARTIAL_LESSON") == 1
        assert current[0].body.count("NEW_FINAL_VERIFIED_RESULT") == 1
        assert set(current[0].sources) >= {
            "codex:shared-session:turn-1:old-proof",
            "codex:shared-session:turn-1:updated-proof",
            "codex:shared-session:turn-1:final-proof",
        }
        completed = agent.memory.source_state(ALICE, CONTEXT)
        assert completed is not None and completed.complete
        assert completed.task_digests == {"source": digest}
        extraction = [material for kind, material in model.events[event_count:] if kind == "extraction"]
        expected_previous = published.body if legacy_format else partial.body
        assert extraction[0]["previous_account"]["body"] == expected_previous
        assert extraction[1]["previous_account"]["body"] == expected_previous
        assert extraction[0]["evidence"] == extraction[1]["evidence"]
        assert not agent.memory.list_summaries(ALICE)[0].stale
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_overlapping_maintenance_for_one_context_retains_both_tasks(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedFirstExtraction(MemoryModel):
        async def run_turn(self, *, output_schema=None, **kwargs):
            assert output_schema is not None
            if "topics" in output_schema["properties"] and not entered.is_set():
                entered.set()
                await release.wait()
            return await super().run_turn(output_schema=output_schema, **kwargs)

    model = PausedFirstExtraction()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        codex=SequentialTurns(new_session_id="shared-session"),
        cheap=model,
        source=MemorySessionSource(
            [
                turn("turn-1", tool("first-proof", "FIRST_VERIFIED_LESSON")),
                turn("turn-2", tool("second-proof", "SECOND_VERIFIED_LESSON")),
            ]
        ),
    )
    try:
        await agent.run_now(continuing_task("first"))
        await asyncio.wait_for(entered.wait(), 5)
        await agent.run_now(continuing_task("second"))
        release.set()
        await complete_memory(agent, "first")
        await agent.sweep_memory()
        await complete_memory(agent, "second")

        sources, summaries = agent.memory.list_sources(ALICE), agent.memory.list_summaries(ALICE)
        assert len(sources) == len(summaries) == 1
        assert sources[0].body.count("FIRST_VERIFIED_LESSON") == 1
        assert sources[0].body.count("SECOND_VERIFIED_LESSON") == 1
        assert sources[0].task_id == "second"
        assert set(sources[0].sources) >= {"task:first", "task:second"}
        assert not summaries[0].stale
        assert summaries[0].sources == (sources[0].id,)
        extraction = [material for kind, material in model.events if kind == "extraction"]
        assert [material["source_task_id"] for material in extraction] == ["first", "second"]
    finally:
        release.set()
        await agent.shutdown()
