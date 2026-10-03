from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nyanpasu.agent import AgentService
from nyanpasu.backends import Backend, Backends
from nyanpasu.config import NyanpasuConfig
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.memory import MemoryAccess, MemoryDenied, MemoryNotFound
from nyanpasu.models import AgentTask, TaskAction, TaskStatus
from tests.session_source import MemorySessionSource
from tests.test_agent import FakeCodex

if TYPE_CHECKING:
    from pathlib import Path

PRIVATE = MemoryAccess(("public", "private:alice"), "private:alice")


def config_for(tmp_path: Path) -> NyanpasuConfig:
    return NyanpasuConfig.model_validate(
        {
            "state_dir": tmp_path / "state",
            "server": {"token": "test-service-token"},
            "backends": {"worker": {"driver": "codex", "defaults": {"model": "original-model"}}},
            "tasks": {"defaults": {"execution": {"backend": "worker"}}},
            "memory": {"consolidate": False},
        }
    )


def make_agent(config: NyanpasuConfig, backend: FakeCodex) -> AgentService:
    return AgentService(config, backends=Backends(config, {"worker": Backend(backend, MemorySessionSource())}))


class Interrupted(FakeCodex):
    async def run_turn(self, **kwargs):
        await super().run_turn(**kwargs)
        raise asyncio.CancelledError


async def interrupt(agent: AgentService, task: AgentTask) -> AgentTask:
    try:
        with pytest.raises(asyncio.CancelledError):
            await agent.run_now(task)
        saved = agent.store.task_request(task.task_id)
        record = agent.store.task_run(task.task_id)
        assert record.status is TaskStatus.QUEUED and record.thread_id is not None
        return saved
    finally:
        await agent.shutdown()


async def recover(agent: AgentService, task_id: str):
    await agent.startup()

    async def finished():
        while True:
            record = agent.store.task_run(task_id)
            if record.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return record
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(finished(), 3)


@pytest.mark.anyio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_recovery_rejects_repurposed_backend_driver(tmp_path: Path, interrupted: bool):
    original = config_for(tmp_path)
    first = make_agent(original, Interrupted())
    task = AgentTask(task_id="original", context_key="context", action=TaskAction.RUN, prompt="work")
    if interrupted:
        saved = await interrupt(first, task)
    else:
        saved = first._admit(task)
        assert first.store.record_task(saved)
        await first.shutdown()
    before = first.store.task_run(task.task_id)
    context = first.store.get_context(task.context_key)
    changed = NyanpasuConfig.model_validate(
        {**original.model_dump(), "backends": {"worker": {"driver": "claude-code"}}}
    )
    backend = FakeCodex()
    restarted = make_agent(changed, backend)
    try:
        result = await recover(restarted, task.task_id)
        assert result.status is TaskStatus.FAILED
        assert result.error is not None and "execution driver differs" in result.error
        assert backend.calls == []
        assert restarted.store.task_request(task.task_id).execution == saved.execution
        assert (result.backend, result.thread_id, result.turn_id) == (before.backend, before.thread_id, before.turn_id)
        assert restarted.store.get_context(task.context_key) == context
        if before.thread_id is not None:
            history = Backends(changed)
            history.register_session_locator(restarted.store.native_home)
            with pytest.raises(ValueError, match="native session driver differs"):
                await history.source("worker").read_metadata(before.thread_id)
            await history.close()
    finally:
        await restarted.shutdown()


@pytest.mark.anyio
async def test_recovery_rejects_changed_native_home_without_losing_saved_session(tmp_path: Path):
    original = config_for(tmp_path)
    first = make_agent(original, Interrupted())
    saved = await interrupt(
        first, AgentTask(task_id="original", context_key="context", action=TaskAction.RUN, prompt="work")
    )
    before = first.store.task_run(saved.task_id)
    assert before.thread_id is not None
    location = first.store.native_home("worker", before.thread_id)
    context = first.store.get_context(saved.context_key)
    assert context is not None and context.session_worktree is not None
    artifact = context.session_worktree / "unfinished.txt"
    artifact.write_text("preserve unfinished work")
    changed = NyanpasuConfig.model_validate(
        {
            **original.model_dump(),
            "backends": {
                "worker": {
                    **original.backends["worker"].model_dump(),
                    "home": {"native_directory": ".moved-native-home"},
                }
            },
        }
    )
    backend = FakeCodex()
    restarted = make_agent(changed, backend)
    try:
        result = await recover(restarted, saved.task_id)
        assert result.status is TaskStatus.FAILED
        assert result.error is not None and "native home configuration changed" in result.error
        assert backend.calls == []
        assert restarted.store.native_home("worker", before.thread_id) == location
        assert restarted.store.get_context(saved.context_key) == context
        assert restarted.store.task_request(saved.task_id).execution == saved.execution
        assert result.thread_id == before.thread_id and result.turn_id == before.turn_id
        assert artifact.read_text() == "preserve unfinished work"
        assert not (location.isolated_home / ".moved-native-home").exists()
    finally:
        await restarted.shutdown()


@pytest.mark.anyio
async def test_disabled_memory_blocks_recovered_task_control_reads_and_writes(tmp_path: Path):
    original = config_for(tmp_path)
    original = original.model_copy(update={"memory": original.memory.model_copy(update={"consolidate": True})})
    first = make_agent(original, Interrupted())
    source = first.memory.checkpoint_source(
        PRIVATE,
        task_id="earlier-source",
        input_digest="a" * 64,
        cursor=1,
        complete=True,
        title="Verified fact",
        body="private knowledge",
        sources=("tool:verified",),
        expected_revision=None,
    )
    saved = await interrupt(
        first,
        AgentTask(task_id="original", context_key="context", action=TaskAction.RUN, prompt="work", memory=PRIVATE),
    )
    disabled = original.model_copy(update={"memory": original.memory.model_copy(update={"enabled": False})})
    checked = []

    class Resumed(FakeCodex):
        async def run_turn(self, **kwargs):
            assert await restarted.control.dispatch(saved.task_id, "memory.search", {"query": "knowledge"}) == []
            assert await restarted.control.dispatch(saved.task_id, "memory.describe", {}) == {
                "count": 0,
                "domains": [],
                "topics": [],
                "navigation_count": 0,
            }
            with pytest.raises(MemoryNotFound):
                await restarted.control.dispatch(saved.task_id, "memory.read", {"source_id": source.id})
            with pytest.raises(MemoryDenied):
                await restarted.control.dispatch(
                    saved.task_id,
                    "memory.write",
                    {"key": "new-fact", "title": "New fact", "body": "must not be saved"},
                )
            with pytest.raises(MemoryDenied):
                await restarted.control.dispatch(
                    saved.task_id, "memory.delete", {"source_id": source.id, "expected_revision": source.revision}
                )
            checked.append(kwargs["thread_id"])
            return await super().run_turn(**kwargs)

    backend = Resumed()
    restarted = make_agent(disabled, backend)
    try:
        result = await recover(restarted, saved.task_id)
        assert result.status is TaskStatus.COMPLETED
        assert checked == [first.store.task_run(saved.task_id).thread_id]
        assert len(backend.calls) == 1
        assert restarted.store.task_request(saved.task_id).memory == PRIVATE
        assert [item.id for item in restarted.memory.list_sources(PRIVATE)] == [source.id]
        assert all(
            item.kind not in {"memory_extraction", "memory_consolidation"} for item in restarted.store.recent_tasks(10)
        )
    finally:
        await restarted.shutdown()


def test_scratch_contexts_do_not_share_or_mount_parent_directories(tmp_path: Path):
    config = config_for(tmp_path)
    manager = WorktreeManager(config)
    tasks = [
        AgentTask(task_id=f"task-{index}", context_key=key, action=TaskAction.RUN, prompt="work")
        for index, key in enumerate(("private:alice", "private-alice", ".", ".."))
    ]
    contexts = [manager.prepare_context(task, None) for task in tasks]
    paths = [context.session_worktree for context in contexts]
    assert len(set(paths)) == len(tasks)
    for index, path in enumerate(paths):
        assert path is not None and path.parent == config.state_dir / "scratch"
        (path / "private.txt").write_text(str(index))
    for index, path in enumerate(paths):
        assert path is not None and (path / "private.txt").read_text() == str(index)
    manager.remove_worktree(None, paths[0])
    assert paths[0] is not None and not paths[0].exists()
    assert all(path is not None and (path / "private.txt").exists() for path in paths[1:])
    assert len({manager.session_worktree_path(task) for task in tasks}) == len(tasks)
    assert len({manager.event_snapshot_path(task) for task in tasks}) == len(tasks)
