from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

import pytest

from nyanpasu import agent as agent_module
from nyanpasu.agent import AgentService
from nyanpasu.codex import CodexTurnError
from nyanpasu.models import SubtaskRequest, TaskAction
from nyanpasu.store import StateStore
from tests.test_agent import FakeCodex, FakeWorktrees, _config, _task, fake_backends
from tests.test_codex import RecordingAppServerBackend, _target
from tests.test_subtask_runtime import wait_status


def overloaded():
    return CodexTurnError("failed", {"message": "Selected model is at capacity.", "codexErrorInfo": "serverOverloaded"})


class CapacityBackend(FakeCodex):
    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures
        self.started_at = []

    async def run_turn(self, **kwargs: Any):
        index = len(self.calls) + 1
        self.started_at.append(time.time())
        notify = kwargs["on_started"]

        async def on_started(thread, turn):
            await notify(thread, f"turn-{index}")

        kwargs["on_started"] = on_started
        result = await super().run_turn(**kwargs)
        marker = kwargs["cwd"] / "earlier-action"
        if index == 1:
            marker.write_text("preserved")
        else:
            assert marker.read_text() == "preserved"
        if index <= self.failures:
            raise overloaded()
        return result.model_copy(update={"turn_id": f"turn-{index}"})


@pytest.mark.anyio
async def test_backend_preserves_structured_capacity_code(tmp_path):
    backend = RecordingAppServerBackend(_config(tmp_path))
    backend._completed_turns[("thread-1", "turn-1")] = {
        "turn": {"status": "failed", "error": {"message": "busy", "codexErrorInfo": "serverOverloaded"}}
    }
    with pytest.raises(CodexTurnError) as error:
        await backend.run_turn(cwd=tmp_path, prompt="work", thread_id=None, execution=_target(backend))
    assert error.value.code == "serverOverloaded"
    assert backend._turn_waiters == {}


@pytest.mark.anyio
async def test_retry_resumes_native_thread_and_preserves_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (0, 0))
    config = _config(tmp_path)
    backend = CapacityBackend()
    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, backend))
    try:
        result = await agent.run_now(_task("retry"))
        record = agent.store.task_run("retry")
        assert result.status == "completed"
        assert [thread for _, thread in backend.calls] == [None, "thread-1"]
        assert record.retry_count == 1 and record.retry_at is None and record.error is None
        assert [turn["turn_id"] for turn in agent.store.task_turns("retry")] == ["turn-1", "turn-2"]
        assert "Check which actions have already completed" in backend.prompts[-1]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_capacity_retries_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (0, 0))
    config = _config(tmp_path)
    backend = CapacityBackend(failures=100)
    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, backend))
    try:
        with pytest.raises(CodexTurnError):
            await agent.run_now(_task("retry"))
        record = agent.store.task_run("retry")
        assert record.status == "failed" and record.retry_count == 2 and record.retry_at is None
        assert len(backend.calls) == 3
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        CodexTurnError("failed", {"message": "Selected model is at capacity.", "codexErrorInfo": "usageLimitExceeded"}),
        CodexTurnError("failed", {"message": "unauthorized", "codexErrorInfo": "other"}),
        RuntimeError("serverOverloaded"),
    ],
)
async def test_unrelated_errors_are_not_retried(tmp_path, error):
    config = _config(tmp_path)

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            raise error

    backend = Backend()
    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, backend))
    try:
        with pytest.raises(type(error)):
            await agent.run_now(_task("failed"))
        assert len(backend.calls) == 1
        assert agent.store.task_run("failed").retry_count == 0
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_backoff_releases_capacity_and_cleanup_cancels_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (60,))
    config = _config(tmp_path, concurrency=1)

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            result = await super().run_turn(**kwargs)
            if "process" in kwargs["prompt"]:
                raise overloaded()
            return result

    backend = Backend()
    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, backend))
    try:
        await agent.submit(_task("retry"))
        await wait_status(agent, "retry", "waiting")
        record = agent.store.task_run("retry")
        assert record.retry_at is not None
        assert record.retry_count == 1 and record.retry_at > time.time() + 50
        assert agent.store.recent_tasks()[0].retry_at == record.retry_at
        item = agent.store.dashboard_snapshot().recent[0]
        assert item.retry_count == 1 and item.retry_at == record.retry_at
        await agent.submit(_task("other", context_key="other").model_copy(update={"prompt": "other"}))
        await wait_status(agent, "other", "completed")
        await agent.submit(_task("cleanup").model_copy(update={"action": TaskAction.CLEANUP}))
        await wait_status(agent, "cleanup", "completed")
        assert agent.store.task_status("retry") == "cancelled"
        assert agent.store.task_run("retry").retry_at is None
        assert len(backend.calls) == 2
        assert not agent.store.unfinished_tasks()
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_restart_keeps_retry_deadline_count_and_session(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (0.4,))
    config = _config(tmp_path)
    first = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, CapacityBackend())
    )
    await first.submit(_task("retry"))
    await wait_status(first, "retry", "waiting")
    before = first.store.task_run("retry")
    assert before.retry_at is not None
    await first.shutdown()
    assert first.store.task_run("retry").retry_at == before.retry_at
    backend = CapacityBackend(failures=0)
    second = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, backend))
    try:
        await second.startup()
        await asyncio.sleep(0.02)
        assert not backend.calls
        await wait_status(second, "retry", "completed")
        assert backend.started_at[0] >= before.retry_at
        assert backend.calls[0][1] == before.thread_id
        assert second.store.task_run("retry").retry_count == 1
    finally:
        await second.shutdown()


@pytest.mark.anyio
async def test_child_capacity_retry_preserves_parent_wait_and_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (0,))
    config = _config(tmp_path, concurrency=1)
    parent_started, parent_release = asyncio.Event(), asyncio.Event()
    child_calls = 0

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            nonlocal child_calls
            result = await super().run_turn(**kwargs)
            if kwargs["prompt"].startswith("parent"):
                if kwargs["thread_id"] is None:
                    parent_started.set()
                    await parent_release.wait()
            elif "Subtask results" not in kwargs["prompt"]:
                child_calls += 1
                if child_calls == 1:
                    raise overloaded()
            return result

    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, Backend()))
    try:
        await agent.submit(_task("parent").model_copy(update={"prompt": "parent"}))
        await asyncio.wait_for(parent_started.wait(), 2)
        child = await agent.create_subtask("parent", SubtaskRequest(request_key="child", prompt="child"))
        await agent.wait_for_subtasks("parent", [child.task_id])
        parent_release.set()
        await wait_status(agent, "parent", "completed")
        assert child_calls == 2
        assert agent.store.task_run(child.task_id).retry_count == 1
        assert agent.store.task_status(child.task_id) == "completed"
        assert not agent.store.pending_subtask_deliveries("parent")
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_parent_backoff_keeps_existing_child_running(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (0.5,))
    config = _config(tmp_path, concurrency=1)
    parent_started, parent_release, child_started, child_release = (asyncio.Event() for _ in range(4))
    child_calls = 0

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            nonlocal child_calls
            result = await super().run_turn(**kwargs)
            if kwargs["cwd"].name == "demo-1":
                if kwargs["thread_id"] is None:
                    parent_started.set()
                    await parent_release.wait()
                    raise overloaded()
            else:
                child_calls += 1
                child_started.set()
                await child_release.wait()
            return result

    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, Backend()))
    try:
        await agent.submit(_task("parent").model_copy(update={"prompt": "parent"}))
        await asyncio.wait_for(parent_started.wait(), 2)
        child = await agent.create_subtask("parent", SubtaskRequest(request_key="child", prompt="child"))
        await asyncio.wait_for(child_started.wait(), 2)
        parent_release.set()
        await wait_status(agent, "parent", "waiting")
        assert agent.store.task_status(child.task_id) == "running"
        child_release.set()
        await wait_status(agent, "parent", "completed")
        assert child_calls == 1
        assert agent.store.task_status(child.task_id) == "completed"
        assert agent.store.task_run("parent").retry_count == 1
        assert not agent.store.pending_subtask_deliveries("parent")
    finally:
        await agent.shutdown()


def test_v5_migration_preserves_execution_history(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    task = _task("old").model_copy(update={"execution": _config(tmp_path).resolve_execution()})
    store.record_task(task)
    store.bind_task_execution("old", "thread", "turn")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE task_runs DROP COLUMN retry_count")
        conn.execute("ALTER TABLE task_runs DROP COLUMN retry_at")
        conn.execute("PRAGMA user_version=5")
    upgraded = StateStore(path)
    assert upgraded.task_run("old").retry_count == 0
    assert upgraded.task_run("old").thread_id == "thread"
    assert upgraded.task_turns("old")[0]["turn_id"] == "turn"


@pytest.mark.anyio
async def test_restart_does_not_reset_exhausted_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "CAPACITY_RETRY_DELAYS", (0,))
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    store.record_task(_task("retry").model_copy(update={"execution": config.resolve_execution()}))
    store.defer_task_retry("retry", str(overloaded()), time.time() - 1)
    backend = CapacityBackend(failures=100)
    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "wt"), backends=fake_backends(config, backend))
    try:
        await agent.startup()
        await wait_status(agent, "retry", "failed")
        assert len(backend.calls) == 1
        assert agent.store.task_run("retry").retry_count == 1
    finally:
        await agent.shutdown()
