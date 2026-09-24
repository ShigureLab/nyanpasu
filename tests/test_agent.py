from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, Mock

import pytest

from nyanpasu.agent import AgentService
from nyanpasu.backends import Backend, Backends
from nyanpasu.config import ClaudeConfig, NyanpasuConfig, RuntimeConfig
from nyanpasu.models import AgentContext, AgentTask, InstructionDocument, RunResult, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu.transcript.codex import CodexHistorySource

if TYPE_CHECKING:
    from nyanpasu.execution import ExecutionStarted


class FakeCodex:
    def __init__(self, *, new_session_id: str = "thread-1") -> None:
        self.new_session_id = new_session_id
        self.calls: list[tuple[Path, str | None]] = []
        self.prompts: list[str] = []
        self.instructions: list[str] = []
        self.archived: list[str] = []

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult:
        self.prompts.append(prompt)
        self.instructions.append(developer_instructions)
        self.calls.append((cwd, thread_id))
        if on_started:
            await on_started(thread_id or self.new_session_id, "turn-1")
        return RunResult(
            thread_id=thread_id or self.new_session_id,
            turn_id="turn-1",
            final_message="done",
        )

    def runtime_info(self):
        return {"connection": "idle", "diagnostics": []}

    async def read_thread(self, thread_id: str) -> dict:
        return {"id": thread_id}

    async def list_turns(self, thread_id: str, cursor: str | None = None) -> dict:
        return {"data": [], "nextCursor": None}

    async def cleanup_thread(self, thread_id: str) -> None:
        self.archived.append(thread_id)

    async def close(self) -> None:
        return None


class FakeWorktrees:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.event_paths: list[Path] = []
        self.removed: list[Path] = []

    def prepare_event_snapshot(self, task: AgentTask) -> Path:
        path = self.root / "events" / task.task_id
        path.mkdir(parents=True)
        self.event_paths.append(path)
        return path

    def prepare_context(self, task: AgentTask, existing: AgentContext | None) -> AgentContext:
        path = self.root / task.context_key.replace(":", "-")
        path.mkdir(parents=True, exist_ok=True)
        return AgentContext(
            context_key=task.context_key,
            thread_id=existing.thread_id if existing else None,
            session_worktree=path,
            workspace_key=task.workspace.key if task.workspace else None,
            revision=task.workspace.revision if task.workspace else None,
        )

    def remove_worktree(self, workspace: WorkspaceRef | None, path: Path | None) -> None:
        _ = workspace
        if path is not None:
            self.removed.append(path)


class SlowCodex(FakeCodex):
    def __init__(
        self, started: asyncio.Event, release: asyncio.Event, second_started: asyncio.Event | None = None
    ) -> None:
        super().__init__()
        self.started = started
        self.release = release
        self.second_started = second_started

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult:
        if not self.calls:
            self.started.set()
        elif self.second_started is not None:
            self.second_started.set()
        await self.release.wait()
        return await super().run_turn(
            cwd=cwd,
            prompt=prompt,
            thread_id=thread_id,
            developer_instructions=developer_instructions,
            on_started=on_started,
        )


class CancellableCodex(FakeCodex):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__()
        self.started = started

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult:
        _ = cwd, prompt, thread_id
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _config(tmp_path: Path, *, concurrency: int = 4) -> NyanpasuConfig:
    return NyanpasuConfig(
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(
            concurrency=concurrency,
            coalesce_window_seconds=600,
            context_lease_seconds=60,
            context_lease_heartbeat_seconds=1,
            context_lease_wait_seconds=0.01,
        ),
    )


def _task(task_id: str, *, context_key: str = "demo:1", revision: str = "abc") -> AgentTask:
    return AgentTask(
        task_id=task_id,
        action=TaskAction.RUN,
        context_key=context_key,
        prompt="process {{NYANPASU_WORKTREE}}",
        workspace=WorkspaceRef(
            key="owner/repo",
            local_path=Path("/repo"),
            remote="https://example.invalid/repo.git",
            ref="refs/heads/main",
            revision=revision,
        ),
        dedupe_key=task_id,
    )


@pytest.mark.anyio
async def test_agent_reuses_context_thread_and_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    codex = FakeCodex()
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    agent = AgentService(config, store=store, worktrees=worktrees, backends=fake_backends(config, codex))

    await agent.run_now(_task("task-1"))
    await agent.run_now(_task("task-2", revision="def"))

    assert codex.calls == [
        (tmp_path / "worktrees" / "demo-1", None),
        (tmp_path / "worktrees" / "demo-1", "thread-1"),
    ]
    assert str(tmp_path / "worktrees" / "demo-1") in codex.prompts[0]
    context = store.get_context("demo:1")
    assert context is not None
    assert context.thread_id == "thread-1"
    assert context.revision == "def"
    assert worktrees.event_paths == []
    assert worktrees.removed == []


@pytest.mark.anyio
async def test_agent_can_opt_into_event_snapshot_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    codex = FakeCodex()
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    agent = AgentService(config, store=store, worktrees=worktrees, backends=fake_backends(config, codex))
    task = _task("task-1").model_copy(
        update={
            "prompt": "process {{NYANPASU_EVENT_WORKTREE}}",
            "workspace_policy": "event_snapshot",
        }
    )

    await agent.run_now(task)

    assert worktrees.event_paths
    assert str(worktrees.event_paths[0]) in codex.prompts[0]
    assert worktrees.removed == worktrees.event_paths


@pytest.mark.anyio
async def test_agent_cleanup_archives_thread_and_deletes_context(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    codex = FakeCodex()
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    agent = AgentService(config, store=store, worktrees=worktrees, backends=fake_backends(config, codex))

    await agent.run_now(_task("task-1"))
    await agent.run_now(
        AgentTask(
            task_id="cleanup-1",
            action=TaskAction.CLEANUP,
            context_key="demo:1",
            prompt="",
            workspace=_task("x").workspace,
        )
    )

    assert codex.archived == ["thread-1"]
    assert store.get_context("demo:1") is None


@pytest.mark.anyio
async def test_agent_coalesces_queued_tasks_for_same_context(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    agent = AgentService(
        config,
        store=store,
        worktrees=FakeWorktrees(tmp_path / "worktrees"),
        backends=fake_backends(config, FakeCodex()),
    )
    agent.add_task_preparer("demo", _prepare_demo)
    assert store.record_task(_merge_task("task-1"))

    result = await agent.submit(_merge_task("task-2"))

    assert result["coalesced"] is True
    assert result["coalesced_into"] == "task-1"
    assert store.task_status("task-2") == "completed"


@pytest.mark.anyio
async def test_agent_queues_one_followup_while_context_is_running_and_coalesces_later_tasks(tmp_path: Path) -> None:
    config = _config(tmp_path, concurrency=2)
    store = StateStore(config.db_path)
    started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()
    codex = SlowCodex(started, release, second_started)
    agent = AgentService(
        config, store=store, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, codex)
    )

    agent.add_task_preparer("demo", _prepare_demo)
    result1 = await agent.submit(_merge_task("task-1"))
    await started.wait()
    result2 = await agent.submit(_merge_task("task-2"))
    result3 = await agent.submit(_merge_task("task-3"))
    release.set()
    await second_started.wait()
    while store.task_status("task-2") != "completed":
        await asyncio.sleep(0)

    assert result1["accepted"] is True
    assert result2["accepted"] is True
    assert result2.get("coalesced") is None
    assert result3["coalesced"] is True
    assert result3["coalesced_into"] == "task-2"
    assert store.task_status("task-1") == "completed"
    assert store.task_status("task-2") == "completed"
    assert store.task_status("task-3") == "completed"
    await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("blocked_by", ["context_lock", "context_lease"])
async def test_waiting_context_does_not_consume_execution_capacity(tmp_path: Path, blocked_by: str) -> None:
    config = _config(tmp_path, concurrency=2 if blocked_by == "context_lock" else 1)
    started = {name: asyncio.Event() for name in ("first", "waiting", "independent", "overflow")}
    release = {name: asyncio.Event() for name in started}

    class GatedCodex(FakeCodex):
        async def run_turn(self, **kwargs):
            name = kwargs["prompt"]
            result = await super().run_turn(**kwargs)
            started[name].set()
            await release[name].wait()
            return result

    agent = AgentService(
        config,
        worktrees=FakeWorktrees(tmp_path / "worktrees"),
        backends=fake_backends(config, GatedCodex()),
    )

    async def submit(name: str, context_key: str = "demo:1") -> None:
        await agent.submit(_task(name, context_key=context_key).model_copy(update={"prompt": name}))

    try:
        if blocked_by == "context_lock":
            await submit("first")
            await asyncio.wait_for(started["first"].wait(), 2)
        else:
            assert agent.store.try_acquire_context_lease(
                "demo:1", owner_id="another-worker", task_id="first", ttl_seconds=60
            )
        await submit("waiting")
        await submit("independent", "demo:2")
        await asyncio.wait_for(started["independent"].wait(), 2)
        assert not started["waiting"].is_set()

        # Execution still obeys the concurrency limit while another context waits.
        await submit("overflow", "demo:3")
        await asyncio.sleep(0.05)
        assert not started["overflow"].is_set()
        release["independent"].set()
        await asyncio.wait_for(started["overflow"].wait(), 2)
        assert not started["waiting"].is_set()
        release["overflow"].set()

        if blocked_by == "context_lock":
            release["first"].set()
        else:
            agent.store.release_context_lease("demo:1", owner_id="another-worker", task_id="first")
        await asyncio.wait_for(started["waiting"].wait(), 2)
        release["waiting"].set()
        await asyncio.wait_for(asyncio.gather(*agent._tasks), 2)
        assert agent.store.unfinished_tasks() == []
        assert all(agent.store.get_context_lease(key) is None for key in ("demo:1", "demo:2", "demo:3"))
    finally:
        await agent.shutdown()
        agent.store.release_context_lease("demo:1", owner_id="another-worker", task_id="first")


@pytest.mark.anyio
async def test_agent_context_lease_serializes_same_context_across_service_instances(tmp_path: Path) -> None:
    config = _config(tmp_path, concurrency=2)
    store = StateStore(config.db_path)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    first_codex = SlowCodex(first_started, release_first)
    second_codex = SlowCodex(second_started, release_second)
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    first_agent = AgentService(config, store=store, worktrees=worktrees, backends=fake_backends(config, first_codex))
    second_agent = AgentService(config, store=store, worktrees=worktrees, backends=fake_backends(config, second_codex))

    first_run = asyncio.create_task(first_agent.run_now(_task("task-1")))
    await first_started.wait()
    second_run = asyncio.create_task(second_agent.run_now(_task("task-2")))
    await asyncio.sleep(0.05)

    assert not second_started.is_set()
    lease = store.get_context_lease("demo:1")
    assert lease is not None
    assert lease.task_id == "task-1"

    release_first.set()
    await first_run
    await second_started.wait()
    lease = store.get_context_lease("demo:1")
    assert lease is not None
    assert lease.task_id == "task-2"
    release_second.set()
    await second_run

    assert store.get_context_lease("demo:1") is None
    await first_agent.shutdown()
    await second_agent.shutdown()


@pytest.mark.anyio
async def test_agent_shutdown_requeues_running_tasks_and_releases_lease(tmp_path: Path) -> None:
    config = _config(tmp_path, concurrency=1)
    store = StateStore(config.db_path)
    started = asyncio.Event()
    agent = AgentService(
        config,
        store=store,
        worktrees=FakeWorktrees(tmp_path / "worktrees"),
        backends=fake_backends(config, CancellableCodex(started)),
    )

    result = await agent.submit(_task("task-1"))
    await started.wait()
    assert result["accepted"] is True
    assert store.task_status("task-1") == "running"
    assert store.get_context_lease("demo:1") is not None

    await agent.shutdown()

    assert store.task_status("task-1") == "queued"
    assert store.get_context_lease("demo:1") is None
    recent = store.recent_tasks()
    assert recent[0].task_id == "task-1"
    assert recent[0].error == "Service stopped; awaiting recovery"


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize("workspace_policy", ["context", "event_snapshot"])
@pytest.mark.parametrize("switch_backend", [False, True])
async def test_restart_resumes_session_in_place_then_runs_queued_task(
    tmp_path: Path, backend, workspace_policy, switch_backend
) -> None:
    config = _config(tmp_path, concurrency=1)
    config = config.model_copy(update={"runtime": config.runtime.model_copy(update={"backend": backend})})
    started = asyncio.Event()

    class InterruptedBackend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            (kwargs["cwd"] / "unfinished.txt").write_text("work in progress")
            started.set()
            await asyncio.Event().wait()

    first_worktrees = FakeWorktrees(tmp_path / "worktrees")
    first = AgentService(
        config, worktrees=first_worktrees, backends=fake_backends(config, InterruptedBackend(), backend)
    )
    request = _task("interrupted").model_copy(
        update={"workspace_policy": workspace_policy, "developer_instructions": "Keep the original role."}
    )
    await first.submit(request)
    await started.wait()
    await first.submit(_task("queued"))
    await first.shutdown()
    assert [task.task_id for task in first.store.unfinished_tasks()] == ["interrupted", "queued"]
    assert first.store.task_run("interrupted").thread_id == "thread-1"
    assert first.store.task_run("interrupted").backend == backend
    original_snapshot = first.store.task_run("interrupted").event_worktree
    if original_snapshot:
        (original_snapshot / "unfinished.txt").write_text("snapshot work in progress")

    if switch_backend:
        backend = "claude" if backend == "codex" else "codex"
        config = config.model_copy(update={"runtime": config.runtime.model_copy(update={"backend": backend})})

    class ResumedBackend(FakeCodex):
        async def run_turn(self, **kwargs):
            assert (kwargs["cwd"] / "unfinished.txt").read_text() == "work in progress"
            if original_snapshot:
                assert (original_snapshot / "unfinished.txt").read_text() == "snapshot work in progress"
            return await super().run_turn(**kwargs)

    session_id = "replacement-session" if switch_backend else "thread-1"
    resumed_backend = ResumedBackend(new_session_id=session_id)
    second_worktrees = FakeWorktrees(tmp_path / "worktrees")
    second_worktrees.prepare_context = Mock(wraps=second_worktrees.prepare_context)
    second = AgentService(config, worktrees=second_worktrees, backends=fake_backends(config, resumed_backend, backend))
    second._prepare_task = AsyncMock(wraps=second._prepare_task)
    await second.startup()
    assert second.store.task_run("queued").backend == backend
    await second.startup()  # Repeated startup must not enqueue the same task twice.
    await asyncio.wait_for(asyncio.gather(*second._tasks), 2)
    await second.shutdown()

    assert [thread for _, thread in resumed_backend.calls] == [None if switch_backend else "thread-1", session_id]
    assert resumed_backend.instructions[0].split("\nNyanpasu subtask control")[0] == "Keep the original role."
    if not switch_backend:
        assert "Continue from the saved conversation" in resumed_backend.prompts[0]
    assert "Continue from the saved conversation" not in resumed_backend.prompts[1]
    assert second_worktrees.event_paths == []  # The interrupted snapshot was reused, not recreated.
    assert [call.args[0].task_id for call in second_worktrees.prepare_context.call_args_list] == ["queued"]
    assert [call.args[0].task_id for call in second._prepare_task.call_args_list] == (
        ["interrupted", "queued"] if switch_backend else ["queued"]
    )
    assert second.store.task_status("interrupted") == second.store.task_status("queued") == "completed"
    assert second.store.task_run("interrupted").error is None
    assert second.store.task_run("interrupted").backend == second.store.task_run("queued").backend == backend
    assert second.store.task_run("interrupted").thread_id == second.store.task_run("queued").thread_id == session_id
    assert len(second.store.recent_tasks()) == 2


@pytest.mark.anyio
async def test_interrupted_backend_switch_keeps_original_session_identity_until_replacement_starts(tmp_path: Path):
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    task = _task("interrupted")
    workspace = tmp_path / "preserved"
    workspace.mkdir()
    context = AgentContext(
        context_key=task.context_key,
        backend="claude",
        thread_id=None,
        session_worktree=workspace,
        workspace_key=None,
        revision=None,
    )
    store.record_task(task, default_backend="claude")
    store.bind_task_execution(task.task_id, "old-claude-session", "old-turn", "claude", context=context)
    started = asyncio.Event()

    class InterruptedStart(FakeCodex):
        async def run_turn(self, **kwargs):
            assert kwargs["thread_id"] is None
            started.set()
            await asyncio.Event().wait()

    first = AgentService(config, store=store, backends=fake_backends(config, InterruptedStart()))
    await first.startup()
    await asyncio.wait_for(started.wait(), 2)
    await first.shutdown()
    record = store.task_run(task.task_id)
    assert (record.backend, record.thread_id, record.turn_id) == ("claude", "old-claude-session", "old-turn")

    replacement = FakeCodex(new_session_id="new-codex-session")
    second = AgentService(config, store=store, backends=fake_backends(config, replacement))
    await second.startup()
    await asyncio.wait_for(asyncio.gather(*second._tasks), 2)
    await second.shutdown()
    assert replacement.calls == [(workspace, None)]
    record = store.task_run(task.task_id)
    assert (record.backend, record.thread_id, record.turn_id) == ("codex", "new-codex-session", "turn-1")


@pytest.mark.anyio
async def test_recovery_waits_for_live_owner_and_does_not_replay_completed_work(tmp_path: Path) -> None:
    config = _config(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    owner = AgentService(
        config,
        worktrees=FakeWorktrees(tmp_path / "worktrees"),
        backends=fake_backends(config, SlowCodex(started, release)),
    )
    candidate_backend = FakeCodex()
    candidate = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, candidate_backend)
    )
    await owner.submit(_task("task"))
    await started.wait()
    await candidate.startup()
    await asyncio.sleep(0.03)
    assert not candidate_backend.calls
    await candidate.shutdown()
    assert owner.store.task_status("task") == "running"
    assert owner.store.task_run("task").error is None
    await candidate.startup()
    release.set()
    await asyncio.wait_for(asyncio.gather(*owner._tasks, *candidate._tasks), 2)
    assert owner.store.task_status("task") == "completed"
    assert not candidate_backend.calls
    await owner.shutdown()
    await candidate.shutdown()


@pytest.mark.anyio
async def test_crash_recovery_waits_for_stale_lease_then_resumes_original_backend(tmp_path: Path) -> None:
    config = _config(tmp_path).model_copy(
        update={"runtime": RuntimeConfig(backend="claude", context_lease_wait_seconds=0.01)}
    )
    state = StateStore(config.db_path)
    task = _task("orphan")
    state.record_task(task, default_backend="claude")
    state.mark_task_running(task.task_id, None, "claude")
    workspace = tmp_path / "preserved"
    workspace.mkdir()
    context = AgentContext(
        backend="claude",
        context_key=task.context_key,
        thread_id="original-session",
        session_worktree=workspace,
        workspace_key=None,
        revision=None,
    )
    state.bind_task_execution(task.task_id, "original-session", "old-turn", "claude", context=context)
    state.try_acquire_context_lease(task.context_key, owner_id="previous-process", task_id=task.task_id, ttl_seconds=60)
    backend = FakeCodex()
    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "unused"), backends=fake_backends(config, backend, "claude")
    )
    await agent.startup()
    await asyncio.sleep(0.03)
    assert not backend.calls
    with state._connect() as conn:
        conn.execute("UPDATE context_leases SET expires_at=0")
    await asyncio.wait_for(asyncio.gather(*agent._tasks), 2)
    assert backend.calls == [(workspace, "original-session")]
    assert state.task_run(task.task_id).backend == "claude"
    assert state.task_status(task.task_id) == "completed"
    await agent.shutdown()


@pytest.mark.anyio
async def test_service_startup_recovers_only_unfinished_tasks(tmp_path: Path) -> None:
    from nyanpasu.plugins import PluginRegistry
    from nyanpasu.web import create_app

    config = _config(tmp_path)
    backend = FakeCodex()
    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, backend)
    )
    await agent.run_now(_task("completed"))
    agent.store.record_task(_task("failed"))
    agent.store.mark_task_failed("failed", "real backend failure")
    agent.store.record_task(_task("queued"))
    app = create_app(config, agent=agent, plugin_registry=PluginRegistry())
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(asyncio.gather(*agent._tasks), 2)
    assert len(backend.calls) == 2
    assert agent.store.task_status("queued") == "completed"
    assert agent.store.task_status("failed") == "failed"


@pytest.mark.anyio
async def test_restart_keeps_prepared_request_and_does_not_run_coalesced_children(tmp_path: Path) -> None:
    config = _config(tmp_path, concurrency=1).model_copy(update={"enabled_plugins": ("demo",)})
    started = asyncio.Event()

    class InterruptedBackend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            started.set()
            await asyncio.Event().wait()

    first = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, InterruptedBackend())
    )
    first.add_task_preparer("demo", _prepare_demo)
    first._semaphore = asyncio.Semaphore(0)
    await first.submit(_merge_task("parent"))
    await first.submit(_merge_task("child"))
    first._semaphore.release()
    await started.wait()
    await first.shutdown()
    backend = FakeCodex()
    second = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, backend)
    )

    async def should_not_prepare(*args):
        raise AssertionError("A started task must use its persisted execution request")

    second.add_task_preparer("demo", should_not_prepare)
    await second.startup()
    await asyncio.wait_for(asyncio.gather(*second._tasks), 2)
    await second.shutdown()
    assert len(backend.calls) == 1
    assert backend.prompts[0].endswith("Task request:\nHandle parent, child")
    assert second.store.task_status("parent") == second.store.task_status("child") == "completed"


@pytest.mark.anyio
async def test_agent_binds_instruction_documents_on_each_resumed_turn(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    codex = FakeCodex()
    agent = AgentService(
        config, store=store, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, codex)
    )
    task = _task("task-1").model_copy(
        update={
            "developer_instructions": "Persistent role.",
            "instruction_docs": (
                InstructionDocument(name="SOUL.md", source=str(tmp_path / "SOUL.md"), content="Stay precise."),
                InstructionDocument(name="AGENTS.md", content="Use project-local conventions."),
            ),
        }
    )

    await agent.run_now(task)

    await agent.run_now(task.model_copy(update={"task_id": "task-2", "dedupe_key": "task-2"}))

    assert (
        codex.instructions[0].split("\nNyanpasu subtask control")[0]
        == codex.instructions[1].split("\nNyanpasu subtask control")[0]
    )
    assert "Persistent role." in codex.instructions[0]
    assert "Configured instruction documents:" in codex.instructions[0]
    assert f"--- SOUL.md ({tmp_path / 'SOUL.md'}) ---" in codex.instructions[0]
    assert "Stay precise." in codex.instructions[0]
    assert "--- AGENTS.md ---" in codex.instructions[0]
    assert all("Stay precise." not in prompt for prompt in codex.prompts)
    assert codex.calls[1][1] == "thread-1"


def _merge_task(task_id: str) -> AgentTask:
    return _task(task_id).model_copy(update={"coalesce_key": "batch", "metadata": {"plugin_id": "demo"}})


async def _prepare_demo(task: AgentTask, coalesced: tuple[AgentTask, ...], context: AgentContext | None) -> AgentTask:
    return task.model_copy(update={"prompt": "Handle " + ", ".join(item.task_id for item in (task, *coalesced))})


@pytest.mark.anyio
async def test_freeform_tasks_are_not_automatically_merged(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    codex = FakeCodex()
    agent = AgentService(
        config, store=store, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, codex)
    )
    agent._semaphore = asyncio.Semaphore(0)
    first = await agent.submit(_task("task-1"))
    second = await agent.submit(_task("task-2"))
    assert "coalesced" not in first and "coalesced" not in second
    agent._semaphore.release()
    while agent._tasks:
        await asyncio.gather(*agent._tasks)
    assert len(codex.calls) == 2
    await agent.shutdown()


def _review_setup(tmp_path: Path, monkeypatch, *, codex: FakeCodex | None = None, config: NyanpasuConfig | None = None):
    import importlib

    from nyanpasu_github_reviewer.models import GitHubReviewerConfig, RepoSettings
    from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin

    head = {"sha": "head-b"}
    module = importlib.import_module("nyanpasu_github_reviewer.plugin")
    monkeypatch.setattr(
        module,
        "gh_json",
        lambda *args, **kwargs: {
            "number": 42,
            "state": "OPEN",
            "isDraft": False,
            "url": "https://github.com/ExampleOrg/ExampleRepo/pull/42",
            "baseRefName": "main",
            "headRefName": "feature",
            "headRefOid": head["sha"],
        },
    )
    plugin = GitHubReviewerPlugin(
        GitHubReviewerConfig(
            repos={"ExampleOrg/ExampleRepo": RepoSettings(local_path=tmp_path / "repo")},
            github_login="review-bot",
            dry_run=True,
        )
    )
    backend = codex or FakeCodex()
    config = config or _config(tmp_path)
    agent = AgentService(
        config,
        worktrees=FakeWorktrees(tmp_path / "worktrees"),
        backends=fake_backends(config, backend, config.runtime.backend),
    )
    plugin.runtime = Mock(config=agent.config)
    agent.add_task_preparer(plugin.id, plugin.prepare_task)
    return agent, plugin, backend, head


def _review_task(plugin, action: str, sha: str) -> AgentTask:
    from nyanpasu_github_reviewer.events import parse_github_event

    event = parse_github_event(
        "pull_request",
        f"{action}-{sha}",
        {
            "action": action,
            "repository": {"full_name": "ExampleOrg/ExampleRepo"},
            "pull_request": {
                "number": 42,
                "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/42",
                "state": "open",
                "draft": False,
                "base": {"ref": "main"},
                "head": {"ref": "feature", "sha": sha},
            },
        },
        agent_login="review-bot",
    )
    return plugin.event_to_task(event)


@pytest.mark.anyio
@pytest.mark.parametrize("reverse", [False, True])
async def test_reviewer_merged_events_produce_one_coherent_turn(tmp_path: Path, monkeypatch, reverse: bool) -> None:
    agent, plugin, codex, _ = _review_setup(tmp_path, monkeypatch)
    agent._semaphore = asyncio.Semaphore(0)
    tasks = [_review_task(plugin, "opened", "head-a"), _review_task(plugin, "synchronize", "head-b")]
    if reverse:
        tasks.reverse()
    await agent.submit(tasks[0])
    merged = await agent.submit(tasks[1])
    assert merged["coalesced"]
    agent._semaphore.release()
    await asyncio.gather(*agent._tasks)

    assert len(codex.prompts) == 1
    assert codex.prompts[0].startswith("Review ")
    assert "Target head: head-b" in codex.prompts[0]
    assert "head-a" not in codex.prompts[0]
    assert "{{NYANPASU" not in codex.prompts[0]
    assert "coalesced_tasks" not in codex.prompts[0]
    assert len(codex.prompts[0]) < 1200
    context = agent.store.get_context(tasks[0].context_key)
    assert context is not None and context.revision == "head-b"
    await agent.shutdown()


@pytest.mark.anyio
async def test_reviewer_events_during_review_resume_with_completed_task_head(tmp_path: Path, monkeypatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    agent, plugin, codex, head = _review_setup(tmp_path, monkeypatch, codex=SlowCodex(started, release))
    head["sha"] = "head-a"
    first = _review_task(plugin, "opened", "head-a")
    await agent.submit(first)
    await started.wait()
    head["sha"] = "head-b"
    await agent.submit(_review_task(plugin, "synchronize", "head-b"))
    head["sha"] = "head-c"
    merged = await agent.submit(_review_task(plugin, "synchronize", "head-c"))
    assert merged["coalesced"]
    release.set()
    await asyncio.gather(*agent._tasks)

    assert len(codex.prompts) == 2
    assert "Target head: head-a" in codex.prompts[0]
    assert "Target head: head-c" in codex.prompts[1]
    assert "Previous task head (not proof of completed review): head-a" in codex.prompts[1]
    assert codex.calls[1][1] == "thread-1"
    assert (
        codex.instructions[0].split("\nNyanpasu subtask control")[0]
        == codex.instructions[1].split("\nNyanpasu subtask control")[0]
    )
    context = agent.store.get_context(first.context_key)
    assert context is not None and context.revision == "head-c"
    await agent.shutdown()


@pytest.mark.anyio
async def test_preparer_can_skip_obsolete_work_without_a_codex_turn(tmp_path: Path) -> None:
    codex = FakeCodex()
    config = _config(tmp_path)
    agent = AgentService(config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, codex))

    async def skip(task, coalesced, context):
        return task.model_copy(update={"action": TaskAction.IGNORED, "prompt": "PR closed while queued."})

    agent.add_task_preparer("demo", skip)
    result = await agent.run_now(_merge_task("skip"))

    assert result.final_message == "PR closed while queued."
    assert result.turn_id is None and codex.calls == []
    assert agent.store.get_context("demo:1") is None
    assert agent.store.recent_tasks()[0].action is TaskAction.IGNORED
    await agent.shutdown()


def fake_backends(config: NyanpasuConfig, execution: FakeCodex, name: str = "codex") -> Backends:
    return Backends(config, {name: Backend(execution, CodexHistorySource(execution))})


@pytest.mark.anyio
@pytest.mark.parametrize("initial_backend", ["codex", "claude"])
async def test_backend_switch_starts_fresh_session_and_preserves_history(
    tmp_path: Path, initial_backend: Literal["codex", "claude"]
):
    from httpx import ASGITransport, AsyncClient

    from nyanpasu.web import create_app

    other_backend: Literal["codex", "claude"] = "claude" if initial_backend == "codex" else "codex"
    store = StateStore(_config(tmp_path).db_path)
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    sessions = set()
    executions = []
    for index, name in enumerate((initial_backend, other_backend, initial_backend)):
        config = _config(tmp_path).model_copy(update={"runtime": RuntimeConfig(backend=name)})
        execution = FakeCodex(new_session_id=f"session-{index}")
        executions.append(execution)
        instances: dict[str, Backend] = {name: Backend(execution, CodexHistorySource(execution))}
        if index:
            previous_name = other_backend if name == initial_backend else initial_backend
            previous = executions[index - 1]
            instances[previous_name] = Backend(previous, CodexHistorySource(previous))
        agent = AgentService(config, store=store, worktrees=worktrees, backends=Backends(config, instances))
        try:
            first = await agent.run_now(_task(f"{index}-first"))
            second = await agent.run_now(_task(f"{index}-second"))
            assert first.backend == second.backend == name
            assert first.thread_id == second.thread_id == f"session-{index}"
            sessions.add((name, first.thread_id))
            context = store.get_context("demo:1")
            assert context and context.backend == name and context.thread_id == first.thread_id
            # Each backend gets a fresh session after a switch, then resumes it normally.
            for position, called in enumerate(executions):
                assert called.calls == [
                    (worktrees.root / "demo-1", None),
                    (worktrees.root / "demo-1", f"session-{position}"),
                ]
                assert called.archived == []
            app = create_app(config, agent=agent)
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                rows = (await client.get("/api/sessions")).json()["items"]
                assert {(row["backend"], row["thread_id"]) for row in rows} == sessions
            assert {(task.backend, task.thread_id) for task in store.recent_tasks()} == sessions
            assert worktrees.removed == []
        finally:
            await agent.shutdown()


@pytest.mark.anyio
async def test_reviewer_backend_switch_uses_fresh_review_and_current_model(tmp_path: Path, monkeypatch):
    config = _config(tmp_path).model_copy(
        update={
            "runtime": RuntimeConfig(backend="claude"),
            "claude": ClaudeConfig(model="claude-test", reasoning_effort="medium"),
        }
    )
    agent, plugin, execution, _ = _review_setup(tmp_path, monkeypatch, config=config)
    task = _review_task(plugin, "synchronize", "head-b")
    agent.store.upsert_context(
        AgentContext(
            context_key=task.context_key,
            backend="codex",
            thread_id="old-codex-session",
            session_worktree=tmp_path / "worktrees",
            workspace_key="ExampleOrg/ExampleRepo",
            revision="head-a",
        )
    )
    try:
        result = await agent.run_now(task)
        assert result.backend == "claude" and execution.calls[0][1] is None
        assert execution.prompts[0].startswith("Review ")
        assert "Previous task head" not in execution.prompts[0]
        assert "Powered by Nyanpasu with claude-test medium" in execution.prompts[0]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("existing_context", [False, True])
async def test_backend_metadata_before_execution_and_after_coalescing(tmp_path: Path, existing_context):
    from httpx import ASGITransport, AsyncClient

    from nyanpasu.web import create_app

    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(backend="claude"),
    )
    agent = AgentService(config)
    agent._semaphore = asyncio.Semaphore(0)  # Keep submissions queued while inspecting their metadata.
    agent.add_task_preparer("demo", _prepare_demo)
    if existing_context:
        agent.store.upsert_context(
            AgentContext(
                context_key="demo:1",
                backend="codex",
                thread_id="codex-session",
                session_worktree=None,
                workspace_key=None,
                revision=None,
            )
        )
    try:
        await agent.submit(_merge_task("queued"))
        assert (await agent.submit(_merge_task("coalesced")))["coalesced_into"] == "queued"
        await agent.submit(_task("ignored").model_copy(update={"action": TaskAction.IGNORED}))
        result = await agent.run_now(_task("ignored-now").model_copy(update={"action": TaskAction.IGNORED}))
        assert result.backend == "claude"
        assert {task.backend for task in agent.store.recent_tasks()} == {"claude"}
        for task_id in ("queued", "coalesced", "ignored", "ignored-now"):
            task = agent.store.find_task_by_dedupe_key(task_id)
            assert task is not None and task.backend == "claude"
        app = create_app(config, agent=agent)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            assert {task["backend"] for task in (await client.get("/tasks")).json()["tasks"]} == {"claude"}
            assert {task["backend"] for task in (await client.get("/api/tasks")).json()["items"]} == {"claude"}
            # The parent's eventual execution binding is the single authority for a coalesced task.
            agent.store.bind_task_execution("queued", "codex-session", "turn", "codex")
            rows = {task["task_id"]: task for task in (await client.get("/api/tasks")).json()["items"]}
            assert rows["coalesced"]["backend"] == rows["queued"]["backend"] == "codex"
            assert rows["coalesced"]["session_id"] == rows["queued"]["session_id"] == "codex-session"
            task = agent.store.find_task_by_dedupe_key("coalesced")
            assert task is not None and task.backend == "codex"
    finally:
        await agent.shutdown()
