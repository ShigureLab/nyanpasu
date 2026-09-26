from __future__ import annotations

import asyncio
import json

import pytest

from nyanpasu.agent import AgentService
from nyanpasu.models import SubtaskRequest
from tests.test_agent import FakeCodex, FakeWorktrees, _config, _task, fake_backends


@pytest.mark.anyio
async def test_children_run_while_parent_runs_and_waits_without_admitting_another_root(tmp_path):
    config = _config(tmp_path, concurrency=1)
    parent_return = asyncio.Event()
    parent_started = asyncio.Event()
    child_started = {name: asyncio.Event() for name in ("one", "two")}
    child_release = asyncio.Event()
    resumed = asyncio.Event()
    other_started = asyncio.Event()

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            result = await super().run_turn(**kwargs)
            prompt = kwargs["prompt"]
            if prompt.startswith("parent") and kwargs["thread_id"] is None:
                parent_started.set()
                await parent_return.wait()
            elif prompt.startswith("parent") or "Subtask results" in prompt:
                resumed.set()
            elif prompt.startswith("other"):
                other_started.set()
            else:
                name = "one" if prompt.startswith("one") else "two"
                child_started[name].set()
                await child_release.wait()
            return result

    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, Backend())
    )
    try:
        await agent.submit(_task("parent").model_copy(update={"prompt": "parent"}))
        await asyncio.wait_for(parent_started.wait(), 2)
        children = [
            await agent.create_subtask("parent", SubtaskRequest(request_key=name, prompt=name))
            for name in child_started
        ]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in child_started.values())), 2)
        await agent.submit(_task("other", context_key="demo:other").model_copy(update={"prompt": "other"}))
        await agent.wait_for_subtasks("parent", [child.task_id for child in children])
        parent_return.set()
        await wait_status(agent, "parent", "waiting")
        assert not other_started.is_set()
        child_release.set()
        await asyncio.wait_for(resumed.wait(), 2)
        await asyncio.wait_for(other_started.wait(), 2)
        await wait_status(agent, "parent", "completed")
        assert all(agent.store.task_status(child.task_id) == "completed" for child in children)
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_cleanup_stops_child_before_removing_its_workspace(tmp_path):
    from nyanpasu.models import TaskAction

    config = _config(tmp_path, concurrency=1)
    parent_started, child_started, child_stopped = (asyncio.Event() for _ in range(3))

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            if kwargs["prompt"].startswith("parent"):
                parent_started.set()
                await asyncio.Event().wait()
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                (kwargs["cwd"] / "stopped").write_text("yes")
                child_stopped.set()

    class Worktrees(FakeWorktrees):
        def remove_worktree(self, workspace, path):
            assert child_stopped.is_set()
            super().remove_worktree(workspace, path)

    worktrees = Worktrees(tmp_path / "worktrees")
    agent = AgentService(config, worktrees=worktrees, backends=fake_backends(config, Backend()))
    try:
        await agent.submit(_task("parent").model_copy(update={"prompt": "parent"}))
        await asyncio.wait_for(parent_started.wait(), 2)
        child = await agent.create_subtask("parent", SubtaskRequest(request_key="child", prompt="child"))
        await asyncio.wait_for(child_started.wait(), 2)
        cleanup = _task("cleanup").model_copy(update={"action": TaskAction.CLEANUP})
        await agent.submit(cleanup)
        await wait_status(agent, "cleanup", "completed")
        assert agent.store.context_scope(child.context_key).lifecycle == "closed"
        assert agent.store.context_scope("demo:1").lifecycle == "closed"
        assert agent.store.task_status("parent") == agent.store.task_status(child.task_id) == "cancelled"
        assert len(worktrees.removed) == 2
    finally:
        await agent.shutdown()


async def wait_status(agent: AgentService, task_id: str, status: str):
    async with asyncio.timeout(3):
        while agent.store.task_status(task_id) != status:
            await asyncio.sleep(0.005)


@pytest.mark.anyio
@pytest.mark.parametrize("turn_finished", [False, True])
async def test_restart_restores_waiting_tree_once_and_preserves_workspaces(tmp_path, turn_finished):
    config = _config(tmp_path, concurrency=1)
    parent_started, child_started = asyncio.Event(), asyncio.Event()

    class Interrupted(FakeCodex):
        async def run_turn(self, **kwargs):
            result = await super().run_turn(**kwargs)
            if kwargs["prompt"] == "parent":
                parent_started.set()
                await parent_release.wait()
                return result
            (kwargs["cwd"] / "unfinished").write_text("keep")
            child_started.set()
            await asyncio.Event().wait()
            return result

    parent_release = asyncio.Event()
    first = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, Interrupted())
    )
    await first.submit(_task("parent").model_copy(update={"prompt": "parent"}))
    await parent_started.wait()
    child = await first.create_subtask("parent", SubtaskRequest(request_key="child", prompt="child"))
    await child_started.wait()
    await first.wait_for_subtasks("parent", [child.task_id])
    if turn_finished:
        parent_release.set()
        await wait_status(first, "parent", "waiting")
    await first.shutdown()
    parent_context = first.store.get_context("demo:1")
    assert parent_context is not None

    class Recovered(FakeCodex):
        async def run_turn(self, **kwargs):
            if kwargs["cwd"] == parent_context.session_worktree:
                assert second.store.task_status(child.task_id) == "completed"
                evidence = json.loads(kwargs["prompt"].split("Subtask results (verify evidence before using):\n")[1])
                assert evidence[0]["result"] == {"summary": "done", "artifacts": [], "data": {}}
            return await super().run_turn(**kwargs)

    backend = Recovered()
    second = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, backend)
    )
    try:
        await second.startup()
        await wait_status(second, "parent", "completed")
        assert second.store.task_status(child.task_id) == "completed"
        assert len(backend.calls) == 2
        child_context = second.store.get_context(child.context_key)
        assert child_context is not None and child_context.session_worktree is not None
        assert (child_context.session_worktree / "unfinished").read_text() == "keep"
        assert any("Subtask results" in prompt for prompt in backend.prompts)
    finally:
        await second.shutdown()


@pytest.mark.anyio
async def test_control_cli_scopes_calls_freezes_evidence_and_revokes_capability(tmp_path, monkeypatch):
    import shlex
    import sys
    from pathlib import Path

    from nyanpasu.task_control import call_control

    config = _config(tmp_path, concurrency=1)
    started, release = asyncio.Event(), asyncio.Event()

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            result = await super().run_turn(**kwargs)
            started.set()
            await release.wait()
            return result

    backend = Backend()
    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, backend)
    )
    try:
        await agent.submit(_task("parent"))
        await started.wait()
        command = next(line for line in backend.instructions[0].splitlines() if line.startswith("Write a JSON"))
        control = Path(shlex.split(command.split("then run: ")[1])[-2])
        capability = json.loads(control.read_text())
        request = tmp_path / "request.json"
        request.write_text(json.dumps({"action": "create", "input": {"request_key": "design", "prompt": "child"}}))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "nyanpasu.task_control", str(control), str(request), stdout=asyncio.subprocess.PIPE
        )
        output, _ = await proc.communicate()
        assert proc.returncode == 0
        child_id = json.loads(output)["task_id"]
        await wait_status(agent, child_id, "running")
        async with asyncio.timeout(3):
            while (context := agent.store.get_context(agent.store.task_request(child_id).context_key)) is None:
                await asyncio.sleep(0.005)
        assert context.session_worktree is not None
        evidence = context.session_worktree / "evidence.json"
        evidence.write_text('{"observed":"failure reproduced"}')
        with pytest.raises(ValueError, match="descendants"):
            await agent.control.dispatch(child_id, "cancel", {"task_ids": ["parent"]})
        destination = config.state_dir / "artifacts" / "subtasks" / child_id
        with pytest.raises(ValueError, match="inside"):
            await agent.control.dispatch(
                child_id, "complete", {"summary": "invalid later file", "artifacts": ["evidence.json", str(request)]}
            )
        assert list(destination.glob("*")) == []
        assert agent.store.subtask_result(child_id) is None

        def unavailable_store(*args):
            raise OSError("result store unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(agent.store, "record_subtask_result", unavailable_store)
            with pytest.raises(OSError, match="result store unavailable"):
                await agent.control.dispatch(
                    child_id, "complete", {"summary": "reproduced", "artifacts": ["evidence.json"]}
                )
        assert list(destination.glob("*")) == []
        result = await agent.control.dispatch(
            child_id, "complete", {"summary": "reproduced", "artifacts": ["evidence.json"]}
        )
        assert (
            await agent.control.dispatch(
                child_id, "complete", {"summary": "reproduced", "artifacts": ["evidence.json"]}
            )
            == result
        )
        evidence.write_text("changed later")
        assert Path(result["artifacts"][0]["path"]).read_text() == '{"observed":"failure reproduced"}'
        with pytest.raises(ValueError, match="frozen"):
            await agent.control.dispatch(child_id, "complete", {"summary": "different", "artifacts": ["evidence.json"]})
        assert list(destination.iterdir()) == [Path(result["artifacts"][0]["path"])]
        with pytest.raises(ValueError, match="inside"):
            await agent.control.dispatch(child_id, "complete", {"summary": "escape", "artifacts": [str(request)]})
        await agent.wait_for_subtasks("parent", [child_id])
        release.set()
        await wait_status(agent, "parent", "completed")
        assert agent.store.subtask_result(child_id) == result
        assert not control.exists()
        # A copy of an old capability also fails after its native turn has ended.
        old = tmp_path / "old.json"
        old.write_text(json.dumps(capability))
        with pytest.raises(ValueError, match="expired"):
            await asyncio.to_thread(call_control, old, {"action": "inspect"})
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_failed_cleanup_is_retried_after_restart_without_resuming_children(tmp_path):
    from nyanpasu.models import TaskAction

    config = _config(tmp_path, concurrency=1)
    worktrees = FakeWorktrees(tmp_path / "worktrees")

    class CannotArchive(FakeCodex):
        async def cleanup_thread(self, thread_id):
            raise RuntimeError("archive unavailable")

    first = AgentService(config, worktrees=worktrees, backends=fake_backends(config, CannotArchive()))
    await first.run_now(_task("parent"))
    await first.submit(_task("cleanup").model_copy(update={"action": TaskAction.CLEANUP}))
    await wait_status(first, "cleanup", "failed")
    assert first.store.context_scope("demo:1").lifecycle == "closing"
    assert worktrees.removed == []
    await first.shutdown()
    backend = FakeCodex()
    second = AgentService(config, worktrees=worktrees, backends=fake_backends(config, backend))
    try:
        await second.startup()
        async with asyncio.timeout(3):
            while second.store.context_scope("demo:1").lifecycle != "closed":
                await asyncio.sleep(0.005)
        assert backend.calls == []
        assert len(worktrees.removed) == 1
    finally:
        await second.shutdown()


@pytest.mark.anyio
async def test_cleanup_joins_workspace_creation_before_removing_files(tmp_path):
    from threading import Event

    from nyanpasu.models import TaskAction

    config = _config(tmp_path, concurrency=1)
    started, release, finished = Event(), Event(), Event()

    class Worktrees(FakeWorktrees):
        def prepare_context(self, task, existing):
            context = super().prepare_context(task, existing)
            started.set()
            assert release.wait(3)
            assert context.session_worktree is not None
            (context.session_worktree / "late-write").write_text("created")
            finished.set()
            return context

        def remove_worktree(self, workspace, path):
            assert finished.is_set()
            super().remove_worktree(workspace, path)

    worktrees, backend = Worktrees(tmp_path / "worktrees"), FakeCodex()
    agent = AgentService(config, worktrees=worktrees, backends=fake_backends(config, backend))
    try:
        await agent.submit(_task("parent"))
        assert await asyncio.to_thread(started.wait, 2)
        await agent.submit(_task("cleanup").model_copy(update={"action": TaskAction.CLEANUP}))
        release.set()
        await wait_status(agent, "cleanup", "completed")
        assert len(worktrees.removed) == 1
        assert backend.calls == []
        assert agent.store.task_status("parent") == "cancelled"
    finally:
        release.set()
        await agent.shutdown()


@pytest.mark.anyio
async def test_backend_start_failure_keeps_workspace_owned_for_cleanup(tmp_path):
    from nyanpasu.models import TaskAction

    class CannotStart(FakeCodex):
        async def run_turn(self, **kwargs):
            raise RuntimeError("backend unavailable")

    config = _config(tmp_path)
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    agent = AgentService(config, worktrees=worktrees, backends=fake_backends(config, CannotStart()))
    try:
        with pytest.raises(RuntimeError, match="backend unavailable"):
            await agent.run_now(_task("parent"))
        await agent.run_now(_task("cleanup").model_copy(update={"action": TaskAction.CLEANUP}))
        assert len(worktrees.removed) == 1
        assert agent.store.get_context("demo:1") is None
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_subtask_cannot_fall_back_to_the_service_working_directory(tmp_path):
    config = _config(tmp_path)
    started = asyncio.Event()

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            started.set()
            await asyncio.Event().wait()

    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, Backend())
    )
    try:
        await agent.submit(_task("parent").model_copy(update={"workspace": None}))
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(ValueError, match="repository workspace"):
            await agent.create_subtask("parent", SubtaskRequest(request_key="child", prompt="child"))
        assert agent.store.subtasks("parent") == []
    finally:
        await agent.shutdown()
