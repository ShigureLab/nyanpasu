from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr, ValidationError

from nyanpasu.agent import AgentService
from nyanpasu.backends import Backend, Backends
from nyanpasu.config import (
    ClaudeBackendConfig,
    CodexBackendConfig,
    MemoryConfig,
    ModelSettings,
    NyanpasuConfig,
    ServerConfig,
    TaskPolicy,
    TasksConfig,
)
from nyanpasu.memory import MemoryAccess, MemoryNotFound, MemoryService
from nyanpasu.memory_consolidation import BLOCK_LIMIT, EVIDENCE_BUDGET, REQUEST_LIMIT
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride
from nyanpasu.task_control import call_control
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_agent import FakeCodex
from tests.test_subtask_runtime import wait_status

if TYPE_CHECKING:
    from pathlib import Path

PUBLIC = MemoryAccess(("public",), "public")
ALICE = MemoryAccess(("public", "private:alice"), "private:alice")
BOB = MemoryAccess(("public", "private:bob"), "private:bob")


def config_for(tmp_path: Path, *, consolidate: bool = False, enabled: bool = True) -> NyanpasuConfig:
    return NyanpasuConfig(
        state_dir=tmp_path / "state",
        server=ServerConfig(token=SecretStr("test-service-token")),
        backends={
            "codex": CodexBackendConfig(defaults=ModelSettings(model="worker-default", reasoning="medium")),
            "cheap": ClaudeBackendConfig(defaults=ModelSettings(model="small-model", reasoning="low")),
        },
        tasks=TasksConfig(
            kinds={
                "review": TaskPolicy(execution=ExecutionOverride(model="review-model", reasoning="high")),
                "audit": TaskPolicy(execution=ExecutionOverride(backend="cheap")),
                "memory_consolidation": TaskPolicy(execution=ExecutionOverride(backend="cheap")),
            }
        ),
        memory=MemoryConfig(enabled=enabled, consolidate=consolidate, max_notes_per_search=2),
    )


def task(name: str, *, memory: MemoryAccess = PUBLIC, **kwargs) -> AgentTask:
    return AgentTask(task_id=name, context_key=name, action=TaskAction.RUN, prompt=name, memory=memory, **kwargs)


def make_agent(config: NyanpasuConfig, *, codex=None, cheap=None, source=None) -> AgentService:
    return AgentService(
        config,
        backends=Backends(
            config,
            {
                "codex": Backend(codex or FakeCodex(new_session_id="codex-session"), source or MemorySessionSource()),
                "cheap": Backend(cheap or FakeCodex(new_session_id="cheap-session"), MemorySessionSource()),
            },
        ),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("access", "enabled", "consolidated"),
    [
        (PUBLIC, True, True),
        (MemoryAccess(("public",)), True, False),
        (None, True, False),
        (PUBLIC, False, False),
    ],
)
async def test_completed_root_routes_memory_work_without_recursion(tmp_path, access, enabled, consolidated):
    config = config_for(tmp_path, consolidate=True, enabled=enabled)
    worker, cheap = FakeCodex(new_session_id="worker-session"), FakeCodex(new_session_id="memory-session")
    source = MemorySessionSource([turn("turn-1", tool("verified-build", "Build exited successfully"))])
    agent = make_agent(config, codex=worker, cheap=cheap, source=source)
    try:
        request = (
            AgentTask(task_id="root", context_key="root", action=TaskAction.RUN, prompt="root", kind="review")
            if access is None
            else task("root", kind="review", memory=access)
        )
        completed = await agent.run_now(request)
        assert agent.store.task_request("root").memory == (access if enabled and access is not None else MemoryAccess())
        assert completed.status is TaskStatus.COMPLETED
        assert [(target.backend, target.model, target.reasoning) for target in worker.executions] == [
            ("codex", "review-model", "high")
        ]
        if consolidated:
            await wait_status(agent, "memory:root", "completed")
            followup = StateStore(config.db_path).task_request("memory:root")
            assert followup.kind == "memory_consolidation"
            assert followup.spawned_by_task_id is None
            assert followup.metadata["memory_source_task_id"] == "root"
            assert followup.memory == access
            assert [(target.backend, target.model, target.reasoning) for target in cheap.executions] == [
                ("cheap", "small-model", "low")
            ]
        else:
            assert agent.store.task_status("memory:root") is None
            assert cheap.calls == []
        assert {item.task_id for item in agent.store.recent_tasks()} == (
            {"root", "memory:root"} if consolidated else {"root"}
        )
        assert agent.store.unfinished_tasks() == []
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_root_completion_and_memory_admission_commit_or_rollback_together(tmp_path):
    config = config_for(tmp_path, consolidate=True)
    agent = make_agent(config)
    store = agent.store
    root = agent._admit(task("root", kind="review"))
    store.record_task(root)
    store.mark_task_running(root.task_id, None)
    followup = agent._memory_followup(root)
    assert followup is not None
    result = TaskRunResult(
        task_id=root.task_id, status=TaskStatus.COMPLETED, thread_id="source", turn_id="turn", final_message="done"
    )
    try:
        with sqlite3.connect(config.db_path) as conn:
            conn.executescript("""
                CREATE TRIGGER reject_memory BEFORE INSERT ON task_runs
                WHEN NEW.task_id = 'memory:root'
                BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END;
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected persistence failure"):
            store.mark_task_done(result, followup=followup)
        reopened = StateStore(config.db_path)
        assert reopened.task_status(root.task_id) == "running"
        assert reopened.task_status(followup.task_id) is None
        with pytest.raises(ValueError, match="unknown context"):
            reopened.context_scope(followup.context_key)
        with sqlite3.connect(config.db_path) as conn:
            conn.execute("DROP TRIGGER reject_memory")
        assert reopened.mark_task_done(result, followup=followup)
        assert reopened.mark_task_done(result, followup=followup)
        reopened = StateStore(config.db_path)
        assert reopened.task_status(root.task_id) == "completed"
        assert reopened.task_request(followup.task_id) == followup
        assert [item.task_id for item in reopened.unfinished_tasks()] == [followup.task_id]
        assert len(reopened.recent_tasks()) == 2
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("oversized", [False, True])
async def test_memory_source_uses_only_bound_turns_and_bounded_tool_evidence(tmp_path, oversized):
    config = config_for(tmp_path, consolidate=True)
    first = MemorySessionSource(
        [
            turn("someone-else", tool("foreign", "PRIVATE UNRELATED TASK")),
            turn("first", tool("failure", "first observed failure")),
            turn(
                "second",
                tool("success", "verified recovery"),
                {"id": "reasoning", "type": "reasoning", "summary": ["INTERNAL REASONING"]},
                {"id": "summary", "type": "agentMessage", "text": "assistant interpretation"},
            ),
        ]
    )
    last_items = (
        [tool(f"large-{index}", f"tool-{index}:" + "evidence" * BLOCK_LIMIT) for index in range(20)]
        if oversized
        else [tool("last-tool", "final observed tool result")]
    )
    last = MemorySessionSource([turn("resumed", *last_items), turn("unbound", tool("later", "OTHER TASK LATER"))])
    agent = AgentService(
        config,
        backends=Backends(
            config,
            {
                "codex": Backend(FakeCodex(), first),
                "cheap": Backend(FakeCodex(), last),
            },
        ),
    )
    root = agent._admit(task("source").model_copy(update={"prompt": "用户约束" * REQUEST_LIMIT}))
    agent.store.record_task(root)
    for backend, thread_id, turn_id in [
        ("codex", "shared", "first"),
        ("codex", "shared", "second"),
        ("cheap", "resumed-session", "resumed"),
    ]:
        agent.store.bind_task_execution(root.task_id, thread_id, turn_id, backend)
    agent.store.mark_task_done(
        TaskRunResult(
            task_id=root.task_id,
            backend="cheap",
            status=TaskStatus.COMPLETED,
            thread_id="resumed-session",
            turn_id="resumed",
            final_message="FINAL SUMMARY WITHOUT EVIDENCE",
        )
    )
    followup = agent._memory_followup(root)
    assert followup is not None
    try:
        prepared = await agent._prepare_task(followup, None)
        material = json.loads(prepared.prompt[prepared.prompt.index("{") :])
        assert material["source_task_id"] == root.task_id
        assert material["source_request"] == root.prompt[:REQUEST_LIMIT]
        evidence = material["evidence"]
        references = {item["reference"] for item in evidence}
        assert all(item["kind"] != "reasoning" for item in evidence)
        assert not any(
            value in json.dumps(material)
            for value in ("PRIVATE UNRELATED TASK", "OTHER TASK LATER", "FINAL SUMMARY WITHOUT EVIDENCE")
        )
        assert [call for call in first.calls if call[0] == "read"] == [("read", "shared", None)]
        assert [call for call in last.calls if call[0] == "read"] == [("read", "resumed-session", None)]
        if oversized:
            assert 0 < len(evidence) < len(last_items) + 3
            assert material["truncated"]
            assert any(item["truncated"] for item in evidence)
            assert all(len(block) <= BLOCK_LIMIT for item in evidence for block in item["blocks"])
            assert sum(len(json.dumps(item, ensure_ascii=False)) for item in evidence) <= EVIDENCE_BUDGET
        else:
            assert references == {
                "codex:shared:first:failure",
                "codex:shared:second:success",
                "codex:shared:second:summary",
                "cheap:resumed-session:resumed:last-tool",
            }
            assert "first observed failure" in json.dumps(evidence)
            assert "final observed tool result" in json.dumps(evidence)
    finally:
        await agent.shutdown()


def note_input(key: str, body: str) -> dict:
    return {"key": key, "title": key, "body": body, "topics": ["testing"], "applies_to": ["repository:Example"]}


@pytest.mark.anyio
async def test_control_capability_filters_reads_and_persists_authorized_merge(tmp_path):
    config = config_for(tmp_path)
    agent = make_agent(config)
    shared = agent.memory.write(PUBLIC, **note_input("shared", "public knowledge"), sources=["tool:public"])
    private = agent.memory.write(ALICE, **note_input("private", "alice knowledge " * 100), sources=["tool:alice"])
    hidden = agent.memory.write(BOB, **note_input("hidden", "bob secret"), sources=["tool:bob"])
    actors = [
        task("off", memory=MemoryAccess()),
        task("reader", memory=MemoryAccess(ALICE.read_domains)),
        task("writer", memory=ALICE, metadata={"memory_source_task_id": "original"}),
    ]
    for actor in actors:
        agent.store.record_task(agent._admit(actor))
        agent.store.mark_task_running(actor.task_id, None)
    try:
        for actor in actors[:2]:
            async with agent.control.turn(actor.task_id) as control:

                async def call(action, payload):
                    return await asyncio.to_thread(call_control, control.file, {"action": action, "input": payload})

                found = await call("memory.search", {"query": "", "limit": 1000})
                assert {note["id"] for note in found} == (
                    {shared.id, private.id} if actor.memory.read_domains else set()
                )
                assert all(len(note["body"]) <= 800 for note in found)
                if actor.memory.read_domains:
                    assert (await call("memory.read", {"note_id": private.id}))["body"] == private.body
                description = await call("memory.describe", {})
                assert "private:bob" not in json.dumps(description)
                with pytest.raises(ValueError, match="memory not found"):
                    await call("memory.read", {"note_id": hidden.id})
                with pytest.raises(ValueError):
                    await call("memory.write", note_input("forbidden", "must not be saved"))
                with pytest.raises(ValueError):
                    await call("memory.search", {"query": "", "domain": "private:bob"})
                with pytest.raises(ValueError):
                    await call("memory.write", {**note_input("forged", "must not be saved"), "access": ALICE.to_dict()})
                if not actor.memory.read_domains:
                    with pytest.raises(ValueError, match="memory not found"):
                        await call("memory.read", {"note_id": shared.id})
        async with agent.control.turn("writer") as control:

            async def call(action, payload):
                return await asyncio.to_thread(call_control, control.file, {"action": action, "input": payload})

            with pytest.raises(ValueError, match="memory not found"):
                await call(
                    "memory.write",
                    {
                        **note_input("shared", "overwrite public"),
                        "note_id": shared.id,
                        "expected_revision": shared.revision,
                    },
                )
            first = await call("memory.write", note_input("canonical", "Run pytest for shared behavior."))
            duplicate = await call("memory.write", note_input("duplicate", "Use the shared pytest fixtures."))
            assert len(await call("memory.search", {"query": "", "limit": 1000})) == config.memory.max_notes_per_search
            merge = {
                **note_input("canonical", "Run pytest with the shared fixtures."),
                "target_id": first["id"],
                "source_ids": [duplicate["id"]],
                "expected_revisions": {first["id"]: first["revision"], duplicate["id"]: duplicate["revision"]},
                "request_key": "merge-source-evidence",
            }
            merged = await call("memory.merge", merge)
            assert await call("memory.merge", merge) == merged
        reopened = MemoryService(config.memory_dir)
        saved = reopened.read(ALICE, first["id"])
        assert saved.to_dict() == merged
        assert saved.domain == "private:alice"
        assert set(saved.sources) == {"task:writer", "task:original"}
        assert saved.merged_from == (duplicate["id"],)
        with pytest.raises(MemoryNotFound):
            reopened.read(ALICE, duplicate["id"])
        with pytest.raises(MemoryNotFound):
            reopened.read(PUBLIC, saved.id)
        assert reopened.read(PUBLIC, shared.id) == shared
        assert reopened.read(BOB, hidden.id) == hidden
        assert len(reopened.list_notes(ALICE)) == 3
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("changed", [MemoryAccess(), MemoryAccess(("public",)), BOB])
async def test_context_cannot_resume_workspace_after_memory_authority_changes(tmp_path, changed):
    config = config_for(tmp_path)
    backend = FakeCodex()
    agent = make_agent(config, codex=backend)
    first = task("first", memory=ALICE)
    try:
        result = await agent.run_now(first)
        assert result.session_worktree is not None
        sentinel = result.session_worktree / "private-result.txt"
        sentinel.write_text("private task artifact")
        context = agent.store.get_context(first.context_key)
        second = task("second", memory=changed).model_copy(update={"context_key": first.context_key})
        with pytest.raises(RuntimeError, match="Memory access changed"):
            await agent.run_now(second)
        assert agent.store.task_status(second.task_id) == "failed"
        assert agent.store.get_context(first.context_key) == context
        assert len(backend.calls) == 1
        assert sentinel.read_text() == "private task artifact"
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_children_resolve_their_own_targets_and_cannot_expand_memory(tmp_path):
    config = config_for(tmp_path)
    started = {name: asyncio.Event() for name in ("parent", "default-child", "independent", "grandchild")}

    class Running(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            started[kwargs["prompt"]].set()
            await asyncio.Event().wait()

    worker, cheap = Running(new_session_id="worker-session"), Running(new_session_id="cheap-session")
    agent = make_agent(config, codex=worker, cheap=cheap)
    parent = task(
        "parent",
        memory=ALICE,
        execution_override=ExecutionOverride(backend="cheap", model="parent-model", reasoning="high"),
    )
    try:
        await agent.submit(parent)
        await asyncio.wait_for(started["parent"].wait(), 3)
        default = await agent.create_subtask(
            parent.task_id, SubtaskRequest(request_key="default", prompt="default-child")
        )
        independent = await agent.create_subtask(
            parent.task_id,
            SubtaskRequest(request_key="independent", prompt="independent", kind="audit", memory_enabled=False),
        )
        await asyncio.wait_for(asyncio.gather(started["default-child"].wait(), started["independent"].wait()), 3)
        grandchild = await agent.create_subtask(
            independent.task_id, SubtaskRequest(request_key="grandchild", prompt="grandchild", memory_enabled=True)
        )
        await asyncio.wait_for(started["grandchild"].wait(), 3)
        assert default.execution is not None and independent.execution is not None
        assert (default.execution.backend, default.execution.model, default.execution.reasoning) == (
            "codex",
            "worker-default",
            "medium",
        )
        assert (independent.execution.backend, independent.execution.model, independent.execution.reasoning) == (
            "cheap",
            "small-model",
            "low",
        )
        assert default.memory == ALICE
        assert independent.memory == grandchild.memory == MemoryAccess()
        assert worker.executions == [default.execution, grandchild.execution]
        assert cheap.executions[-1] == independent.execution
        with pytest.raises(ValidationError):
            await agent.control.dispatch(
                independent.task_id, "create", {"request_key": "forged", "prompt": "forged", "memory": ALICE.to_dict()}
            )
        assert len(agent.store.subtasks(parent.task_id)) == 3
    finally:
        await agent.shutdown()
