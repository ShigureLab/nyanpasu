from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nyanpasu.agent import AgentService
from nyanpasu.config import NyanpasuConfig, RuntimeConfig
from nyanpasu.models import AgentContext, AgentTask, CodexRunResult, InstructionDocument, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore


class FakeCodex:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str | None]] = []
        self.prompts: list[str] = []
        self.archived: list[str] = []

    async def run_turn(self, *, cwd: Path, prompt: str, thread_id: str | None) -> CodexRunResult:
        self.prompts.append(prompt)
        self.calls.append((cwd, thread_id))
        return CodexRunResult(
            thread_id=thread_id or "thread-1",
            turn_id="turn-1",
            final_message="done",
            raw_events=[],
        )

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

    async def run_turn(self, *, cwd: Path, prompt: str, thread_id: str | None) -> CodexRunResult:
        if not self.calls:
            self.started.set()
        elif self.second_started is not None:
            self.second_started.set()
        await self.release.wait()
        return await super().run_turn(cwd=cwd, prompt=prompt, thread_id=thread_id)


class CancellableCodex(FakeCodex):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__()
        self.started = started

    async def run_turn(self, *, cwd: Path, prompt: str, thread_id: str | None) -> CodexRunResult:
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
    agent = AgentService(config, store=store, worktrees=worktrees, codex=codex)

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
    agent = AgentService(config, store=store, worktrees=worktrees, codex=codex)
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
    agent = AgentService(config, store=store, worktrees=worktrees, codex=codex)

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
    agent = AgentService(config, store=store, worktrees=FakeWorktrees(tmp_path / "worktrees"), codex=FakeCodex())
    assert store.record_task(_task("task-1"))

    result = await agent.submit(_task("task-2"))

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
    agent = AgentService(config, store=store, worktrees=FakeWorktrees(tmp_path / "worktrees"), codex=codex)

    result1 = await agent.submit(_task("task-1"))
    await started.wait()
    result2 = await agent.submit(_task("task-2"))
    result3 = await agent.submit(_task("task-3"))
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
    first_agent = AgentService(config, store=store, worktrees=worktrees, codex=first_codex)
    second_agent = AgentService(config, store=store, worktrees=worktrees, codex=second_codex)

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
async def test_agent_shutdown_marks_running_tasks_failed_and_releases_lease(tmp_path: Path) -> None:
    config = _config(tmp_path, concurrency=1)
    store = StateStore(config.db_path)
    started = asyncio.Event()
    agent = AgentService(
        config,
        store=store,
        worktrees=FakeWorktrees(tmp_path / "worktrees"),
        codex=CancellableCodex(started),
    )

    result = await agent.submit(_task("task-1"))
    await started.wait()
    assert result["accepted"] is True
    assert store.task_status("task-1") == "running"
    assert store.get_context_lease("demo:1") is not None

    await agent.shutdown()

    assert store.task_status("task-1") == "failed"
    assert store.get_context_lease("demo:1") is None
    recent = store.recent_tasks()
    assert recent[0].task_id == "task-1"
    assert recent[0].error == "task interrupted by agent shutdown"


@pytest.mark.anyio
async def test_agent_appends_task_instruction_documents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.db_path)
    codex = FakeCodex()
    agent = AgentService(config, store=store, worktrees=FakeWorktrees(tmp_path / "worktrees"), codex=codex)
    task = _task("task-1").model_copy(
        update={
            "instruction_docs": (
                InstructionDocument(name="SOUL.md", source=str(tmp_path / "SOUL.md"), content="Stay precise."),
                InstructionDocument(name="AGENTS.md", content="Use project-local conventions."),
            )
        }
    )

    await agent.run_now(task)

    assert "Task-specific instruction documents:" in codex.prompts[0]
    assert f"--- SOUL.md ({tmp_path / 'SOUL.md'}) ---" in codex.prompts[0]
    assert "Stay precise." in codex.prompts[0]
    assert "--- AGENTS.md ---" in codex.prompts[0]
