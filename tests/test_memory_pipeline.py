from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from dataclasses import asdict

import pytest

from nyanpasu.memory import SUMMARY_MAX_BYTES, MemoryAccess, MemoryDenied, MemoryNotFound, MemoryService, summary_block
from nyanpasu.memory_consolidation import BLOCK_LIMIT, EVIDENCE_BUDGET, MEMORY_TASK_KINDS, input_digest
from nyanpasu.memory_context import MEMORY_CONTEXT_MAX_BYTES, build_memory_context
from nyanpasu.models import TaskRunResult, TaskStatus
from nyanpasu.task_control import call_control
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_agent import FakeCodex
from tests.test_hybrid_memory import ALICE, BOB, PUBLIC, config_for, make_agent, task

ROOT_EXTRACTION_ID = f"memory:{hashlib.sha256(b'root').hexdigest()}"
SOURCE_EXTRACTION_ID = f"memory:{hashlib.sha256(b'source').hexdigest()}"


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
        self.events.append(("extraction" if extraction else "summary", material))
        number = len(self.events)
        if number in self.fail_at:
            raise RuntimeError("injected model failure")
        if number in self.responses:
            return result.model_copy(update={"final_message": self.responses[number]})
        if extraction:
            previous = material["previous_account"] or {}
            snippets = [item["text"][:64] for item in material["evidence"]]
            output = {
                "title": "Source task history",
                "body": previous.get("body", "") + "\n" + "\n".join(snippets),
                "topics": ["testing"],
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


def publish_source(
    service,
    access,
    source_id,
    *,
    body="verified source history",
    title=None,
    context_key=None,
    context_generation=1,
    source_order=0,
    **overrides,
):
    fields = {
        "context_key": context_key or source_id,
        "context_generation": context_generation,
        "source_order": source_order,
        "input_digest": f"digest:{source_id}",
        "cursor": 1,
        "complete": True,
        "title": title or source_id,
        "body": body,
        "topics": ["testing"],
        "sources": [f"tool:{source_id}"],
    }
    return service.checkpoint_source(access, source_id, **(fields | overrides))


def publish_summary(service, access, body, *, context_key="source", context_generation=1):
    snapshot = service.snapshot_context(access, context_key, context_generation)
    return service.publish_summary(
        access,
        context_key,
        context_generation,
        body=body,
        source_ids=[source.id for source in snapshot.sources],
        source_revisions=snapshot.source_revisions,
        input_digest=snapshot.input_digest,
        expected_revision=snapshot.summary.revision if snapshot.summary else None,
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
    worker = FakeCodex(new_session_id="codex-session")
    agent = make_agent(config, codex=worker, cheap=model, source=source)
    controls = []
    injected = []
    original_turn = agent.control.turn

    @contextlib.asynccontextmanager
    async def recorded_control(identity):
        controls.append(identity)
        async with original_turn(identity) as control:
            injected.append(json.loads(json.dumps(asdict(control.memory_context))))
            yield control

    monkeypatch.setattr(agent.control, "turn", recorded_control)
    publish_source(agent.memory, PUBLIC, "shared-unrelated", body="PUBLIC NAV MUST NOT ENTER PRIVATE MAINTENANCE")
    try:
        completed = await agent.run_now(task("root", kind="review", memory=access))
        assert completed.status is TaskStatus.COMPLETED
        if maintained:
            last = await agent.wait_for_memory(ROOT_EXTRACTION_ID)
            assert last.status is TaskStatus.COMPLETED and last.kind == "memory_consolidation"
            assert [kind for kind, _ in model.events] == ["extraction", "summary"]
            extraction = agent.store.task_request(ROOT_EXTRACTION_ID)
            assert extraction.memory == MemoryAccess((access.write_domain,), access.write_domain)
            assert extraction.execution is not None
            assert extraction.execution.backend == "cheap"
            assert all(thread_id is None for _, thread_id in model.calls)
            assert all("Nyanpasu task control" not in text for text in model.instructions)
            assert "PUBLIC NAV MUST NOT ENTER PRIVATE MAINTENANCE" not in json.dumps(model.events)
            checkpoint = agent.memory.source_state(access, "root")
            assert checkpoint is not None and checkpoint.complete
            recalled = agent.memory.read(access, checkpoint.id)
            assert "Build exited successfully" in recalled.body
            assert agent.memory.list_summaries(access)[-1].sources == (recalled.id,)
            assert {entry.task_id for entry in agent.store.recent_tasks()} == {
                "root",
                ROOT_EXTRACTION_ID,
                f"{ROOT_EXTRACTION_ID}:summary",
            }
        else:
            assert agent.store.task_status(ROOT_EXTRACTION_ID) is None
            assert model.events == []
        assert controls == ["root"]
        saved_turns = agent.store.task_turns("root")
        assert len(saved_turns) == 1
        assert json.loads(saved_turns[0]["memory_context_json"]) == injected[0]
        assert injected[0]["prompt"] in worker.instructions[0]
        assert injected[0]["bytes"] == len(injected[0]["prompt"].encode("utf-8")) <= MEMORY_CONTEXT_MAX_BYTES
        for background in agent.store.recent_tasks():
            if background.kind in MEMORY_TASK_KINDS:
                assert all(row["memory_context_json"] is None for row in agent.store.task_turns(background.task_id))
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
        failed = await agent.wait_for_memory(SOURCE_EXTRACTION_ID)
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
        assert "piece-0" in saved.body and "piece-5" in saved.body
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
        result = await agent.wait_for_memory(SOURCE_EXTRACTION_ID)
        assert result.status is TaskStatus.FAILED
        assert agent.store.task_status("source") == "completed"
        assert agent.memory.source_state(PUBLIC, "source") is None
        assert agent.store.task_status(f"{SOURCE_EXTRACTION_ID}:summary") is None
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_service_preserves_all_input_references_without_model_copying(tmp_path):
    model = MemoryModel()
    long_id = "call_4600894c412c44868c8833f3"
    paths = "/workspace/review-output.md and /workspace/scope-review.md"
    records = [tool(long_id, f"Read {paths}")]
    records += [tool(f"call-{index}", f"Verified result {index}") for index in range(130)]
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", *records)]),
    )
    # The model can retain paths and even imperfect identifiers in prose; source
    # metadata must come from the input records, not text it happens to produce.
    prose = f"Read {paths}; the assistant mentioned call_4600894c412c44868c8833f."
    model.responses[1] = json.dumps({"title": "Review history", "body": prose, "topics": ["review"]})
    try:
        await agent.run_now(task("source", memory=ALICE))
        assert (await agent.wait_for_memory(SOURCE_EXTRACTION_ID)).status is TaskStatus.COMPLETED
        saved = agent.memory.source_state(ALICE, "source")
        assert saved is not None and saved.complete
        expected = {f"codex:codex-session:turn-1:call-{index}" for index in range(130)}
        expected |= {f"codex:codex-session:turn-1:{long_id}", "task:source"}
        assert set(saved.sources) == expected
        assert paths in saved.body
        assert all(
            "reference" not in item
            for kind, material in model.events
            if kind == "extraction"
            for item in material["evidence"]
        )
        assert all(
            "sources" not in (material["previous_account"] or {})
            for kind, material in model.events
            if kind == "extraction"
        )
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_resume_reconstructs_provenance_missing_from_prior_checkpoint(tmp_path):
    model = MemoryModel()
    records = [tool(f"call-{index}", f"piece-{index}:" + "x" * 40_000) for index in range(8)]
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", *records)]),
    )
    request = agent._admit(task("source", memory=ALICE))
    agent.store.record_task(request)
    agent.store.bind_task_execution(request.task_id, "codex-session", "turn-1", "codex")
    agent.store.mark_task_done(
        TaskRunResult(
            task_id=request.task_id,
            status=TaskStatus.COMPLETED,
            backend="codex",
            thread_id="codex-session",
            turn_id="turn-1",
            final_message="done",
        )
    )
    try:
        chunks = await agent._source_chunks(request)
        assert len(chunks) > 5
        checkpoint = agent.memory.checkpoint_source(
            ALICE,
            request.task_id,
            input_digest=input_digest(chunks),
            cursor=5,
            complete=False,
            title="Earlier partial account",
            body="Already processed five chunks",
            topics=["review"],
            sources=[],
        )
        assert checkpoint.sources == ("task:source",)
        retry = await agent.rebuild_memory(request.task_id)
        assert (await agent.wait_for_memory(retry.task_id)).status is TaskStatus.COMPLETED
        extraction = [material for kind, material in model.events if kind == "extraction"]
        assert len(extraction) == len(chunks) - 5
        assert extraction[0]["previous_account"]["body"] == checkpoint.body
        assert extraction[0]["evidence"][0]["text"] == chunks[5][0]["text"]
        saved = agent.memory.source_state(ALICE, request.task_id)
        assert saved is not None and saved.complete and saved.cursor == len(chunks)
        assert set(saved.sources) == {"task:source"} | {
            f"codex:codex-session:turn-1:call-{index}" for index in range(8)
        }
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_summary_failure_retries_only_summary_and_preserves_prior_summary(tmp_path):
    model = MemoryModel()
    model.fail_at.add(2)
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified result"))]),
    )
    old = publish_source(agent.memory, PUBLIC, "older", context_key="source")
    prior = publish_summary(agent.memory, PUBLIC, f"[Older task](memory:{old.id})")
    try:
        await agent.run_now(task("source"))
        failed = await agent.wait_for_memory(SOURCE_EXTRACTION_ID)
        assert failed.kind == "memory_consolidation" and failed.status is TaskStatus.FAILED
        unchanged = agent.memory.list_summaries(PUBLIC)[0]
        assert unchanged.revision == prior.revision and unchanged.stale
        retry = await agent.rebuild_memory("source")
        assert retry.kind == "memory_consolidation"
        assert (await agent.wait_for_memory(retry.task_id)).status is TaskStatus.COMPLETED
        assert [kind for kind, _ in model.events].count("extraction") == 1
        assert agent.memory.list_summaries(PUBLIC)[0].stale is False
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
    extraction_id = SOURCE_EXTRACTION_ID

    def stop_after_commit(result, **kwargs):
        if result.task_id == extraction_id:
            raise asyncio.CancelledError
        return original_done(result, **kwargs)

    monkeypatch.setattr(agent.store, "mark_task_done", stop_after_commit)
    try:
        await agent.run_now(task("source"))
        for _ in range(200):
            checkpoint = agent.memory.source_state(PUBLIC, "source")
            if checkpoint is not None and checkpoint.complete and agent.store.task_status(extraction_id) == "queued":
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
        last = await recovered.wait_for_memory(extraction_id)
        assert last.status is TaskStatus.COMPLETED
        assert last.task_id == f"{extraction_id}:summary"
        assert [kind for kind, _ in model.events] == ["extraction", "summary"]
        assert len(recovered.memory.list_sources(PUBLIC)) == 1
    finally:
        await recovered.shutdown()


@pytest.mark.anyio
async def test_model_memory_capabilities_are_read_only_and_domain_scoped(tmp_path):
    agent = make_agent(config_for(tmp_path))
    shared = publish_source(agent.memory, PUBLIC, "shared", body="public history")
    private = publish_source(agent.memory, ALICE, "private", body="alice history " * 100)
    hidden = publish_source(agent.memory, BOB, "hidden", body="bob private history")
    public_summary = publish_summary(agent.memory, PUBLIC, "PUBLIC SUMMARY", context_key="shared")
    private_summary = publish_summary(agent.memory, ALICE, "ALICE SUMMARY", context_key="private")
    publish_summary(agent.memory, BOB, "BOB SUMMARY", context_key="hidden")
    actors = [
        task("off", memory=MemoryAccess()),
        task("reader", memory=MemoryAccess(ALICE.read_domains)),
        task("contributor", memory=ALICE),
    ]
    actors = [
        actor.model_copy(update={"metadata": {"memory_related_contexts": ["shared", "private", "hidden"]}})
        for actor in actors
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
                assert "BOB SUMMARY" not in control.prompt
                if actor.memory.read_domains:
                    assert "ALICE SUMMARY" in control.prompt and "PUBLIC SUMMARY" in control.prompt
                    assert {entry.id for entry in control.memory_context.selected} == {
                        public_summary.id,
                        private_summary.id,
                    }
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
        rendered = build_memory_context(actors[1], agent.memory.list_summaries(ALICE))
        assert len(rendered.prompt.encode("utf-8")) == rendered.bytes <= MEMORY_CONTEXT_MAX_BYTES
        assert {entry.id for entry in rendered.selected} == {public_summary.id, private_summary.id}
        assert len(MemoryService(agent.config.memory_dir).list_sources(ALICE)) == 2
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["submit", "run_now"])
async def test_ordinary_admission_cannot_claim_generated_memory_task_ids(tmp_path, operation):
    model = FakeCodex()
    agent = make_agent(config_for(tmp_path), codex=model)
    claimed_id = "memory:source:summary"
    try:
        with pytest.raises(MemoryDenied, match="reserved"):
            await getattr(agent, operation)(task(claimed_id, memory=MemoryAccess()))
        assert agent.store.task_status(claimed_id) is None
        assert model.calls == []
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_source_names_cannot_collide_with_another_sources_summary(tmp_path):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified result"))]),
    )
    jobs = set()
    try:
        for source_id in ("source", "source:summary"):
            await agent.run_now(task(source_id))
            extractions = [
                item
                for item in agent.store.recent_tasks()
                if item.kind == "memory_extraction"
                and agent.store.task_request(item.task_id).metadata["memory_source_task_id"] == source_id
            ]
            assert len(extractions) == 1
            last = await agent.wait_for_memory(extractions[0].task_id)
            assert last.status is TaskStatus.COMPLETED and last.kind == "memory_consolidation"
            jobs.update((extractions[0].task_id, last.task_id))
        assert len(jobs) == 4
        assert {source.task_id for source in agent.memory.list_sources(PUBLIC)} == {"source", "source:summary"}
        assert [kind for kind, _ in model.events] == ["extraction", "summary", "extraction", "summary"]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_background_kinds_cannot_be_requested_and_have_no_control(tmp_path):
    agent = make_agent(config_for(tmp_path))
    agent.store.record_task(agent._admit(task("source")))
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
async def test_summary_consumes_only_its_context_in_bounded_batches(tmp_path):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified result"))]),
    )
    prior = [
        publish_source(
            agent.memory, ALICE, f"history-{index}", body="history " * 1600, context_key="source", source_order=index
        )
        for index in range(7)
    ]
    hidden = publish_source(agent.memory, PUBLIC, "outside-domain", body="PUBLIC HISTORY OUTSIDE MAINTENANCE DOMAIN")
    unrelated = publish_source(agent.memory, ALICE, "another-context", body="PRIVATE UNRELATED CONTEXT")
    next_generation = publish_source(agent.memory, ALICE, "new-generation", context_key="source", context_generation=2)
    try:
        await agent.run_now(task("source", memory=ALICE))
        assert (await agent.wait_for_memory(SOURCE_EXTRACTION_ID)).status is TaskStatus.COMPLETED
        batches = [material["source_accounts"] for kind, material in model.events if kind == "summary"]
        assert len(batches) > 1
        seen = [source["id"] for batch in batches for source in batch]
        snapshot = agent.memory.snapshot_context(ALICE, "source", 1)
        assert set(seen) == set(snapshot.source_revisions)
        assert len(seen) == len(set(seen)) == len(prior) + 1
        assert hidden.id not in seen
        assert unrelated.id not in seen and next_generation.id not in seen
        assert seen[: len(prior)] == [source.id for source in prior]
        assert {material["context_key"] for kind, material in model.events if kind == "summary"} == {"source"}
        assert all(
            sum(len(json.dumps(item, ensure_ascii=False)) for item in batch) <= EVIDENCE_BUDGET for batch in batches
        )
        assert snapshot.summary is not None
        assert set(snapshot.summary.sources) == set(seen)
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_summary_appends_only_new_sources_to_the_previous_context_summary(tmp_path):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified follow-up result"))]),
    )
    old = publish_source(agent.memory, PUBLIC, "older", context_key="source", source_order=10)
    prior = publish_summary(agent.memory, PUBLIC, f"# Prior result\n[Verified history](memory:{old.id})")
    unrelated = publish_source(agent.memory, PUBLIC, "unrelated", body="DO NOT REPLAY ANOTHER CONTEXT")
    unrelated_summary = publish_summary(agent.memory, PUBLIC, "Unrelated summary", context_key="unrelated")
    try:
        await agent.run_now(task("source"))
        assert (await agent.wait_for_memory(SOURCE_EXTRACTION_ID)).status is TaskStatus.COMPLETED
        current = agent.memory.source_state(PUBLIC, "source")
        assert current is not None
        calls = [material for kind, material in model.events if kind == "summary"]
        assert len(calls) == 1
        assert calls[0]["draft"] == {"body": prior.body, "source_ids": list(prior.sources)}
        assert [item["id"] for item in calls[0]["source_accounts"]] == [current.id]
        assert "DO NOT REPLAY ANOTHER CONTEXT" not in json.dumps(model.events)
        snapshot = agent.memory.snapshot_context(PUBLIC, "source", 1)
        assert snapshot.summary is not None and not snapshot.summary.stale
        assert snapshot.summary.source_revisions == {old.id: old.revision, current.id: current.revision}
        assert set(snapshot.summary.sources) == {old.id, current.id}
        assert agent.memory.snapshot_context(PUBLIC, unrelated.context_key, 1).summary == unrelated_summary
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["corrected", "withdrawn", "inserted-earlier"])
async def test_changed_historical_sources_replay_only_their_context(tmp_path, change):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "current verified result"))]),
    )
    old = publish_source(agent.memory, PUBLIC, "older", context_key="source", source_order=10)
    publish_source(agent.memory, PUBLIC, "unrelated", body="UNRELATED EVIDENCE MUST NOT BE REPLAYED")
    other = publish_summary(agent.memory, PUBLIC, "Untouched other context", context_key="unrelated")
    try:
        await agent.run_now(task("source"))
        assert (await agent.wait_for_memory(SOURCE_EXTRACTION_ID)).status is TaskStatus.COMPLETED
        if change == "inserted-earlier":
            inserted = publish_source(
                agent.memory, PUBLIC, "inserted", context_key="source", source_order=5, body="Earlier evidence"
            )
        else:
            publish_source(
                agent.memory,
                PUBLIC,
                old.task_id,
                context_key=old.context_key,
                source_order=old.source_order,
                input_digest="corrected-evidence",
                body="Verified correction" if change == "corrected" else "",
                expected_revision=old.revision,
            )
        expected = agent.memory.snapshot_context(PUBLIC, "source", 1)
        assert expected.summary is not None and expected.summary.stale
        model.events.clear()
        retry = await agent.rebuild_memory("source")
        assert (await agent.wait_for_memory(retry.task_id)).status is TaskStatus.COMPLETED
        assert [kind for kind, _ in model.events] == ["summary"]
        material = model.events[0][1]
        assert material["draft"] == {"body": "", "source_ids": []}
        assert [item["id"] for item in material["source_accounts"]] == [source.id for source in expected.sources]
        assert "UNRELATED EVIDENCE MUST NOT BE REPLAYED" not in json.dumps(material)
        if change == "inserted-earlier":
            assert material["source_accounts"][0]["id"] == inserted.id
        elif change == "corrected":
            assert material["source_accounts"][0]["body"] == "Verified correction"
        else:
            assert old.id not in {item["id"] for item in material["source_accounts"]}
        summary = agent.memory.snapshot_context(PUBLIC, "source", 1).summary
        assert summary is not None and not summary.stale
        assert summary.source_revisions == expected.source_revisions
        assert agent.memory.snapshot_context(PUBLIC, "unrelated", 1).summary == other
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_noop_followup_can_preserve_prior_useful_summary_while_advancing_coverage(tmp_path):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("noop", "No additional verified changes"))]),
    )
    old = publish_source(
        agent.memory, PUBLIC, "older", context_key="source", body="Recovery was verified at revision abc"
    )
    prior = publish_summary(agent.memory, PUBLIC, f"# Recovery\nVerified at revision abc. [Evidence](memory:{old.id})")
    # This fixture exercises carried-forward evidence and publication, not the
    # model's ability to decide whether a natural-language change is a no-op.
    model.responses[2] = json.dumps({"body": prior.body, "source_ids": list(prior.sources)})
    try:
        await agent.run_now(task("source"))
        assert (await agent.wait_for_memory(SOURCE_EXTRACTION_ID)).status is TaskStatus.COMPLETED
        material = next(material for kind, material in model.events if kind == "summary")
        assert material["draft"] == {"body": prior.body, "source_ids": list(prior.sources)}
        current = agent.memory.source_state(PUBLIC, "source")
        assert current is not None
        assert [item["id"] for item in material["source_accounts"]] == [current.id]
        snapshot = agent.memory.snapshot_context(PUBLIC, "source", 1)
        assert snapshot.summary is not None
        assert snapshot.summary.body == prior.body and snapshot.summary.sources == prior.sources
        assert snapshot.summary.source_revisions == {old.id: old.revision, current.id: current.revision}
        assert not snapshot.summary.stale
        calls = len(model.events)
        retry = await agent.rebuild_memory("source")
        assert (await agent.wait_for_memory(retry.task_id)).status is TaskStatus.COMPLETED
        assert len(model.events) == calls
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("compresses", [True, False])
async def test_oversized_summary_gets_one_compression_attempt_and_preserves_atomic_publication(tmp_path, compresses):
    model = MemoryModel()
    agent = make_agent(
        config_for(tmp_path, consolidate=True),
        cheap=model,
        source=MemorySessionSource([turn("turn-1", tool("proof", "verified follow-up"))]),
    )
    old = publish_source(agent.memory, PUBLIC, "older", context_key="source")
    prior = publish_summary(agent.memory, PUBLIC, f"# Prior verified result\n[Evidence](memory:{old.id})")
    model.responses[2] = json.dumps({"body": "汉" * 400, "source_ids": [old.id]})
    replacement = f"# Updated history\n[Verified evidence](memory:{old.id})"
    model.responses[3] = json.dumps({"body": replacement if compresses else "🙂" * 300, "source_ids": [old.id]})
    try:
        await agent.run_now(task("source"))
        result = await agent.wait_for_memory(SOURCE_EXTRACTION_ID)
        assert result.status is (TaskStatus.COMPLETED if compresses else TaskStatus.FAILED)
        assert [kind for kind, _ in model.events] == ["extraction", "summary", "summary"]
        original, retry = model.events[1][1], model.events[2][1]
        assert original["size_feedback"] is None and retry["size_feedback"]
        assert retry["draft"] == original["draft"]
        assert retry["source_accounts"] == original["source_accounts"]
        assert retry["context_key"] == original["context_key"] == "source"
        snapshot = agent.memory.snapshot_context(PUBLIC, "source", 1)
        assert snapshot.summary is not None
        source = agent.memory.source_state(PUBLIC, "source")
        assert source is not None and source.complete
        if compresses:
            assert snapshot.summary.body == replacement
            assert snapshot.summary.revision != prior.revision and not snapshot.summary.stale
            assert len(summary_block(snapshot.summary).encode("utf-8")) <= SUMMARY_MAX_BYTES
        else:
            assert snapshot.summary.revision == prior.revision and snapshot.summary.stale
            assert snapshot.summary.body == prior.body
            assert result.error is not None and "1024" in result.error
    finally:
        await agent.shutdown()
