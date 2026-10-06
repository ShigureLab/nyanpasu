from __future__ import annotations

import asyncio
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
from nyanpasu.memory import MemoryAccess, MemoryDenied
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride
from tests.session_source import MemorySessionSource
from tests.test_agent import FakeCodex

if TYPE_CHECKING:
    from pathlib import Path

PUBLIC = MemoryAccess(("public",), "public")
ALICE = MemoryAccess(("public", "private:alice"), "private:alice")
BOB = MemoryAccess(("public", "private:bob"), "private:bob")


def config_for(
    tmp_path: Path,
    *,
    consolidate: bool = False,
    enabled: bool = True,
    idle_after_seconds: float = 0,
    sweep_interval_seconds: float = 60,
) -> NyanpasuConfig:
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
                "memory_extraction": TaskPolicy(execution=ExecutionOverride(backend="cheap")),
                "memory_consolidation": TaskPolicy(execution=ExecutionOverride(backend="cheap")),
            }
        ),
        memory=MemoryConfig(
            enabled=enabled,
            consolidate=consolidate,
            idle_after_seconds=idle_after_seconds,
            sweep_interval_seconds=sweep_interval_seconds,
            max_results_per_search=2,
        ),
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
async def test_extraction_completion_and_summary_admission_commit_or_rollback_together(tmp_path):
    config = config_for(tmp_path, consolidate=True)
    agent = make_agent(config)
    store = agent.store
    source = agent._admit(task("source", kind="review"))
    store.record_task(source)
    store.mark_task_done(
        TaskRunResult(
            task_id=source.task_id, status=TaskStatus.COMPLETED, thread_id=None, turn_id=None, final_message="done"
        )
    )
    agent.memory.checkpoint_source(
        PUBLIC, source.task_id, input_digest="source-input", cursor=1, complete=True, title="Lesson", body="Useful fact"
    )
    root = agent._memory_task(source.task_id, "public", task_id="memory:extraction", kind="memory_extraction")
    store.record_task(root)
    store.mark_task_running(root.task_id, None)
    followup = agent._memory_followup(root)
    assert followup is not None
    result = TaskRunResult(
        task_id=root.task_id, status=TaskStatus.COMPLETED, thread_id="source", turn_id="turn", final_message="done"
    )
    try:
        with sqlite3.connect(config.db_path) as conn:
            conn.executescript(f"""
                CREATE TRIGGER reject_memory BEFORE INSERT ON task_runs
                WHEN NEW.task_id = '{followup.task_id}'
                BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END;
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected persistence failure"):
            store.mark_task_done(result, followup=followup)
        reopened = StateStore(config.db_path)
        assert reopened.task_status(root.task_id) == "running"
        assert reopened.task_status(followup.task_id) is None
        assert reopened.context_scope(followup.context_key) == store.context_scope(root.context_key)
        with sqlite3.connect(config.db_path) as conn:
            conn.execute("DROP TRIGGER reject_memory")
        assert reopened.mark_task_done(result, followup=followup)
        assert reopened.mark_task_done(result, followup=followup)
        reopened = StateStore(config.db_path)
        assert reopened.task_status(root.task_id) == "completed"
        assert reopened.task_request(followup.task_id) == followup
        assert [item.task_id for item in reopened.unfinished_tasks()] == [followup.task_id]
        assert len(reopened.recent_tasks()) == 3
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
        for kind in ("memory_extraction", "memory_consolidation"):
            with pytest.raises(MemoryDenied, match="cannot be created"):
                await agent.create_subtask(
                    parent.task_id, SubtaskRequest(request_key=kind, prompt="forged background job", kind=kind)
                )
        assert len(agent.store.subtasks(parent.task_id)) == 3
    finally:
        await agent.shutdown()
