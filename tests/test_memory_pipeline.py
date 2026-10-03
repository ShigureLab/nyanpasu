from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from nyanpasu.memory import MemoryAccess, MemoryDenied, MemoryNotFound, MemoryService
from nyanpasu.memory_consolidation import BLOCK_LIMIT, EVIDENCE_BUDGET, MEMORY_TASK_KINDS
from nyanpasu.models import TaskStatus
from nyanpasu.task_control import MEMORY_NAVIGATION_BUDGET, call_control, navigation_context
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_agent import FakeCodex
from tests.test_hybrid_memory import ALICE, BOB, PUBLIC, config_for, make_agent, task


class MemoryModel(FakeCodex):
    """A JSON-producing external model with observable inputs and injected failures."""

    def __init__(self):
        super().__init__(new_session_id="memory-model")
        self.events = []
        self.fail_at: set[int] = set()
        self.responses: dict[int, str] = {}

    async def run_turn(self, *, output_schema=None, **kwargs):
        assert output_schema is not None
        result = await super().run_turn(**kwargs)
        material = json.loads(kwargs["prompt"][kwargs["prompt"].index("{") :])
        extraction = "topics" in output_schema["properties"]
        self.events.append(("extraction" if extraction else "navigation", material))
        number = len(self.events)
        if number in self.fail_at:
            raise RuntimeError("injected model failure")
        if number in self.responses:
            return result.model_copy(update={"final_message": self.responses[number]})
        if extraction:
            previous = material["previous_account"] or {}
            references = sorted(set(previous.get("sources", [])) | {item["reference"] for item in material["evidence"]})
            output = {
                "title": "Source task history",
                "body": "Retained evidence: " + ", ".join(references),
                "topics": ["testing"],
                "sources": references,
            }
        else:
            references = sorted(
                set(material["draft"]["source_ids"]) | {item["id"] for item in material["source_accounts"]}
            )
            output = {
                "body": "\n".join(f"- [Prior task](memory:{identity})" for identity in references),
                "source_ids": references,
            }
        return result.model_copy(update={"final_message": json.dumps(output)})


def publish_source(service, access, source_id, *, body="verified source history", title=None):
    return service.checkpoint_source(
        access,
        source_id,
        input_digest=f"digest:{source_id}",
        cursor=1,
        complete=True,
        title=title or source_id,
        body=body,
        topics=["testing"],
        sources=[f"tool:{source_id}"],
    )


def publish_navigation(service, access, body):
    snapshot = service.snapshot_domain(access)
    return service.publish_navigation(
        access,
        body=body,
        source_ids=[source.id for source in snapshot.sources],
        source_revisions=snapshot.source_revisions,
        input_digest=snapshot.input_digest,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("access", "enabled", "maintained"),
    [
        (ALICE, True, True),
        (MemoryAccess(("public",)), True, False),
        (MemoryAccess(), True, False),
        (PUBLIC, False, False),
    ],
)
async def test_completed_root_runs_read_only_background_pipeline(tmp_path, monkeypatch, access, enabled, maintained):
    config = config_for(tmp_path, consolidate=True, enabled=enabled)
    model = MemoryModel()
    source = MemorySessionSource([turn("turn-1", tool("verified-build", "Build exited successfully"))])
    agent = make_agent(config, cheap=model, source=source)
    controls = []
    original_turn = agent.control.turn

    @contextlib.asynccontextmanager
    async def recorded_control(identity):
        controls.append(identity)
        async with original_turn(identity) as control:
            yield control

    monkeypatch.setattr(agent.control, "turn", recorded_control)
    publish_source(agent.memory, PUBLIC, "shared-unrelated", body="PUBLIC NAV MUST NOT ENTER PRIVATE MAINTENANCE")
    try:
        completed = await agent.run_now(task("root", kind="review", memory=access))
        assert completed.status is TaskStatus.COMPLETED
        if maintained:
            last = await agent.wait_for_memory("memory:root")
            assert last.status is TaskStatus.COMPLETED and last.kind == "memory_consolidation"
            assert [kind for kind, _ in model.events] == ["extraction", "navigation"]
            extraction = agent.store.task_request("memory:root")
            assert extraction.memory == MemoryAccess((access.write_domain,), access.write_domain)
            assert extraction.execution is not None
            assert extraction.execution.backend == "cheap"
            assert all(thread_id is None for _, thread_id in model.calls)
            assert all("Nyanpasu task control" not in text for text in model.instructions)
            assert "PUBLIC NAV MUST NOT ENTER PRIVATE MAINTENANCE" not in json.dumps(model.events)
            checkpoint = agent.memory.source_state(access, "root")
            assert checkpoint is not None and checkpoint.complete
            recalled = agent.memory.read(access, checkpoint.id)
            assert "verified-build" in recalled.body
            assert agent.memory.list_navigation(access)[-1].sources == (recalled.id,)
            assert {entry.task_id for entry in agent.store.recent_tasks()} == {
                "root",
                "memory:root",
                "memory:root:navigation",
            }
        else:
            assert agent.store.task_status("memory:root") is None
            assert model.events == []
        assert controls == ["root"]
        assert agent.store.unfinished_tasks() == []
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_extraction_retains_ordered_bound_evidence_and_resumes_committed_chunks(tmp_path):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    model.fail_at.add(2)
    records = [tool(f"proof-{index}", f"piece-{index}:" + "evidence" * BLOCK_LIMIT) for index in range(6)]
    source = MemorySessionSource(
        [turn("unrelated", tool("foreign", "PRIVATE UNBOUND HISTORY")), turn("turn-1", *records)]
    )
    agent = make_agent(config, cheap=model, source=source)
    try:
        await agent.run_now(task("source", memory=ALICE))
        failed = await agent.wait_for_memory("memory:source")
        assert failed.status is TaskStatus.FAILED
        checkpoint = agent.memory.source_state(ALICE, "source")
        assert checkpoint is not None
        assert checkpoint.cursor == 1 and not checkpoint.complete
        assert agent.memory.list_sources(ALICE) == []
        with pytest.raises(MemoryNotFound):
            agent.memory.read(ALICE, checkpoint.id)
        first_chunk = model.events[0][1]["evidence"]
        unfinished_chunk = model.events[1][1]["evidence"]
        retry = await agent.rebuild_memory("source")
        completed = await agent.wait_for_memory(retry.task_id)
        assert completed.status is TaskStatus.COMPLETED
        assert model.events[2][1]["evidence"] == unfinished_chunk
        assert model.events[2][1]["previous_account"]["body"] == checkpoint.body
        chunks = [material["evidence"] for kind, material in model.events if kind == "extraction"]
        assert sum(chunk == first_chunk for chunk in chunks) == 1
        assert all(
            sum(len(json.dumps(item, ensure_ascii=False)) for item in chunk) <= EVIDENCE_BUDGET for chunk in chunks
        )
        assert "PRIVATE UNBOUND HISTORY" not in json.dumps(model.events)
        # The last verified evidence survives even after processing many earlier chunks.
        saved = agent.memory.source_state(ALICE, "source")
        assert saved is not None and saved.complete and saved.cursor > 2
        assert "proof-0" in saved.body and "proof-5" in saved.body
        assert saved.id == checkpoint.id
        assert len(agent.memory.list_sources(ALICE)) == 1
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response", ["not JSON", json.dumps({"title": "bad", "body": "x", "topics": [], "sources": ["other:secret"]})]
)
async def test_invalid_model_output_never_commits_a_source(tmp_path, response):
    model = MemoryModel()
    model.responses[1] = response
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified result"))]),
    )
    try:
        await agent.run_now(task("source"))
        result = await agent.wait_for_memory("memory:source")
        assert result.status is TaskStatus.FAILED
        assert agent.store.task_status("source") == "completed"
        assert agent.memory.source_state(PUBLIC, "source") is None
        assert agent.store.task_status("memory:source:navigation") is None
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_navigation_failure_retries_only_navigation_and_preserves_prior_summary(tmp_path):
    model = MemoryModel()
    model.fail_at.add(2)
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified result"))]),
    )
    old = publish_source(agent.memory, PUBLIC, "older")
    prior = publish_navigation(agent.memory, PUBLIC, f"[Older task](memory:{old.id})")
    try:
        await agent.run_now(task("source"))
        failed = await agent.wait_for_memory("memory:source")
        assert failed.kind == "memory_consolidation" and failed.status is TaskStatus.FAILED
        unchanged = agent.memory.list_navigation(PUBLIC)[0]
        assert unchanged.revision == prior.revision and unchanged.stale
        retry = await agent.rebuild_memory("source")
        assert retry.kind == "memory_consolidation"
        assert (await agent.wait_for_memory(retry.task_id)).status is TaskStatus.COMPLETED
        assert [kind for kind, _ in model.events].count("extraction") == 1
        assert agent.memory.list_navigation(PUBLIC)[0].stale is False
        assert agent.store.task_status(failed.task_id) == "failed"
        # An unchanged rebuild reuses the publication receipt and makes no model call.
        calls = len(model.events)
        again = await agent.rebuild_memory("source")
        assert (await agent.wait_for_memory(again.task_id)).status is TaskStatus.COMPLETED
        assert len(model.events) == calls
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_restart_after_source_commit_recovers_without_regeneration(tmp_path, monkeypatch):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    source = MemorySessionSource([turn("turn-1", tool("proof", "verified result"))])
    agent = make_agent(config, cheap=model, source=source)
    original_done = agent.store.mark_task_done

    def stop_after_commit(result, **kwargs):
        if result.task_id == "memory:source":
            raise asyncio.CancelledError
        return original_done(result, **kwargs)

    monkeypatch.setattr(agent.store, "mark_task_done", stop_after_commit)
    try:
        await agent.run_now(task("source"))
        for _ in range(200):
            checkpoint = agent.memory.source_state(PUBLIC, "source")
            if checkpoint is not None and checkpoint.complete and agent.store.task_status("memory:source") == "queued":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("source checkpoint was not committed before the interruption")
        assert [kind for kind, _ in model.events] == ["extraction"]
    finally:
        await agent.shutdown()
    recovered = make_agent(config, cheap=model, source=source)
    try:
        await recovered.startup()
        assert (await recovered.wait_for_memory("memory:source")).status is TaskStatus.COMPLETED
        assert [kind for kind, _ in model.events] == ["extraction", "navigation"]
        assert len(recovered.memory.list_sources(PUBLIC)) == 1
    finally:
        await recovered.shutdown()


@pytest.mark.anyio
async def test_model_memory_capabilities_are_read_only_and_domain_scoped(tmp_path):
    agent = make_agent(config_for(tmp_path))
    shared = publish_source(agent.memory, PUBLIC, "shared", body="public history")
    private = publish_source(agent.memory, ALICE, "private", body="alice history " * 100)
    hidden = publish_source(agent.memory, BOB, "hidden", body="bob private history")
    publish_navigation(agent.memory, PUBLIC, "PUBLIC NAV " + "p" * 6900)
    publish_navigation(agent.memory, ALICE, "ALICE NAV " + "a" * 6900)
    publish_navigation(agent.memory, BOB, "BOB NAV")
    actors = [
        task("off", memory=MemoryAccess()),
        task("reader", memory=MemoryAccess(ALICE.read_domains)),
        task("contributor", memory=ALICE),
    ]
    for actor in actors:
        agent.store.record_task(agent._admit(actor))
        agent.store.mark_task_running(actor.task_id, None)
    try:
        for actor in actors:
            async with agent.control.turn(actor.task_id) as control:

                async def call(action, payload):
                    return await asyncio.to_thread(call_control, control.file, {"action": action, "input": payload})

                found = await call("memory.search", {"query": "", "limit": 1000})
                assert {item["id"] for item in found} == (
                    {shared.id, private.id} if actor.memory.read_domains else set()
                )
                assert all(len(item["body"]) <= 800 for item in found)
                assert "BOB NAV" not in control.prompt
                if actor.memory.read_domains:
                    assert "ALICE NAV" in control.prompt and "PUBLIC NAV" in control.prompt
                    assert (await call("memory.read", {"source_id": private.id}))["body"] == private.body
                with pytest.raises(ValueError, match="not found"):
                    await call("memory.read", {"source_id": hidden.id})
                for action in ("memory.write", "memory.merge", "memory.delete"):
                    with pytest.raises(ValueError, match="read-only"):
                        await call(action, {"source_id": shared.id, "access": ALICE.to_dict()})
                with pytest.raises(ValueError):
                    await call("memory.search", {"query": "", "domain": "private:bob"})
                with pytest.raises(ValueError):
                    await call("memory.read", {"note_id": private.id})
        rendered = navigation_context(agent.memory.list_navigation(ALICE))
        assert len(rendered) <= MEMORY_NAVIGATION_BUDGET
        assert {entry["domain"] for entry in json.loads(rendered)} == set(ALICE.read_domains)
        assert len(MemoryService(agent.config.memory_dir).list_sources(ALICE)) == 2
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_background_kinds_cannot_be_requested_and_have_no_control(tmp_path):
    agent = make_agent(config_for(tmp_path))
    try:
        for kind in MEMORY_TASK_KINDS:
            with pytest.raises(MemoryDenied, match="reserved"):
                await agent.run_now(task(f"forged-{kind}", kind=kind))
            background = agent._memory_task("source", "public", task_id=kind, kind=kind)
            agent.store.record_task(background)
            agent.store.mark_task_running(background.task_id, None)
            with pytest.raises(MemoryDenied, match="no task control"):
                async with agent.control.turn(background.task_id):
                    pytest.fail("background jobs cannot receive a control capability")
            for action in ("create", "inspect", "memory.read", "memory.write"):
                with pytest.raises(MemoryDenied, match="no task control"):
                    await agent.control.dispatch(background.task_id, action, {})
        assert not agent.control._tokens
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_extraction_reads_only_bound_turns_across_backend_handoffs(tmp_path):
    from nyanpasu.backends import Backend, Backends

    config = config_for(tmp_path)
    first = MemorySessionSource(
        [
            turn("unrelated", tool("foreign", "PRIVATE UNRELATED TASK")),
            turn("first", tool("failure", "first observed failure")),
            turn(
                "second",
                tool("success", "verified recovery"),
                {"id": "reasoning", "type": "reasoning", "summary": ["INTERNAL REASONING"]},
                {"id": "summary", "type": "agentMessage", "text": "assistant interpretation"},
            ),
        ]
    )
    last = MemorySessionSource(
        [
            turn("resumed", tool("last", "final observed tool result")),
            turn("unbound", tool("later", "OTHER TASK LATER")),
        ]
    )
    agent = make_agent(config)
    agent.backends = Backends(
        config,
        {
            "codex": Backend(FakeCodex(), first),
            "cheap": Backend(FakeCodex(), last),
        },
    )
    request = agent._admit(task("source", memory=ALICE))
    agent.store.record_task(request)
    for backend, session, identity in [
        ("codex", "shared", "first"),
        ("codex", "shared", "second"),
        ("cheap", "resumed-session", "resumed"),
    ]:
        agent.store.bind_task_execution(request.task_id, session, identity, backend)
    try:
        chunks = await agent._source_chunks(request)
        material = json.dumps(chunks)
        assert all(
            value not in material for value in ("PRIVATE UNRELATED TASK", "OTHER TASK LATER", "INTERNAL REASONING")
        )
        references = {item["reference"] for chunk in chunks for item in chunk}
        assert references == {
            "task:source",
            "codex:shared:first:failure",
            "codex:shared:second:success",
            "codex:shared:second:summary",
            "cheap:resumed-session:resumed:last",
        }
        assert (
            material.index("first observed failure")
            < material.index("verified recovery")
            < material.index("final observed tool result")
        )
        assert [call for call in first.calls if call[0] == "read"] == [("read", "shared", None)]
        assert [call for call in last.calls if call[0] == "read"] == [("read", "resumed-session", None)]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_navigation_consumes_the_whole_domain_in_bounded_batches(tmp_path):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified result"))]),
    )
    prior = [publish_source(agent.memory, ALICE, f"history-{index}", body="history " * 1600) for index in range(7)]
    hidden = publish_source(agent.memory, PUBLIC, "outside-domain", body="PUBLIC HISTORY OUTSIDE MAINTENANCE DOMAIN")
    try:
        await agent.run_now(task("source", memory=ALICE))
        assert (await agent.wait_for_memory("memory:source")).status is TaskStatus.COMPLETED
        batches = [material["source_accounts"] for kind, material in model.events if kind == "navigation"]
        assert len(batches) > 1
        seen = [source["id"] for batch in batches for source in batch]
        snapshot = agent.memory.snapshot_domain(ALICE)
        assert set(seen) == set(snapshot.source_revisions)
        assert len(seen) == len(set(seen)) == len(prior) + 1
        assert hidden.id not in seen
        assert all(
            sum(len(json.dumps(item, ensure_ascii=False)) for item in batch) <= EVIDENCE_BUDGET for batch in batches
        )
        assert snapshot.navigation is not None
        assert set(snapshot.navigation.sources) == set(seen)
    finally:
        await agent.shutdown()
