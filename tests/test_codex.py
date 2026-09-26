from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from nyanpasu.codex import CodexAppServerBackend, safe_codex_env
from nyanpasu.config import CodexConfig, NyanpasuConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_safe_codex_env_filters_by_default_and_honors_pass_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("GH_TOKEN", "secret-gh-token")
    monkeypatch.setenv("NYANPASU_TEST_ALLOWED", "allowed")

    config = NyanpasuConfig(state_dir=tmp_path / "state", codex=CodexConfig(pass_env=("NYANPASU_TEST_ALLOWED",)))

    env = safe_codex_env(config)

    assert env["PATH"] == "/usr/bin"
    assert env["NYANPASU_TEST_ALLOWED"] == "allowed"
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_app_server_backend_completes_from_task_complete_payload(tmp_path: Path) -> None:
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    backend = CodexAppServerBackend(config)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        waiter = loop.create_future()
        backend._turn_waiters[("thread-1", "turn-1")] = waiter

        backend._handle_message(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "done",
                },
            }
        )

        completed = waiter.result()
        assert completed["threadId"] == "thread-1"
        assert completed["turn"]["status"] == "completed"
        assert completed["turn"]["items"] == [{"type": "agentMessage", "text": "done"}]
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_app_server_backend_resets_dead_process_state(tmp_path: Path) -> None:
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    backend = CodexAppServerBackend(config)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        waiter = loop.create_future()
        backend._proc = loop.run_until_complete(asyncio.create_subprocess_exec("true"))
        loop.run_until_complete(backend._proc.wait())
        backend._pending[1] = waiter
        backend._agent_messages[("thread-1", "turn-1")] = ["stale"]

        alive = loop.run_until_complete(backend._process_alive())

        assert alive is False
        assert backend._proc is None
        assert backend._reader_task is None
        assert backend._pending == {}
        assert backend._agent_messages == {}
        assert waiter.done()
        assert isinstance(waiter.exception(), RuntimeError)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_app_server_requests_include_approvals_reviewer(tmp_path: Path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        codex=CodexConfig(
            approval_policy="on-request",
            approvals_reviewer="auto_review",
            model="configured-model",
            reasoning_effort="medium",
        ),
    )
    backend = RecordingAppServerBackend(config)

    async def run() -> None:
        backend._completed_turns[("thread-1", "turn-1")] = {
            "threadId": "thread-1",
            "turn": {"status": "completed", "items": [{"type": "agentMessage", "text": "done"}]},
        }
        await backend.run_turn(cwd=tmp_path, prompt="review", thread_id=None, developer_instructions="review role")

        backend._completed_turns[("thread-1", "turn-1")] = {
            "threadId": "thread-1",
            "turn": {"status": "completed", "items": [{"type": "agentMessage", "text": "done"}]},
        }
        await backend.run_turn(
            cwd=tmp_path, prompt="new commit", thread_id="thread-1", developer_instructions="updated review role"
        )

    asyncio.run(run())

    assert [method for method, _ in backend.requests] == [
        "thread/start",
        "turn/start",
        "thread/resume",
        "turn/start",
    ]
    for _, params in backend.requests:
        assert params["model"] == "configured-model"
        assert params["approvalPolicy"] == "on-request"
        assert params["approvalsReviewer"] == "auto_review"
        assert "runtimeWorkspaceRoots" not in params
        assert "persistExtendedHistory" not in params
        assert "experimentalRawEvents" not in params
        assert "baseInstructions" not in params
    assert backend.requests[0][1]["developerInstructions"] == "review role"
    assert backend.requests[2][1]["developerInstructions"] == "updated review role"
    for _, params in backend.requests[::2]:
        assert params["config"] == {"model_reasoning_effort": "medium"}
    for (_, params), text in zip(backend.requests[1::2], ("review", "new commit"), strict=True):
        assert params["effort"] == "medium"
        assert params["input"] == [{"type": "text", "text": text, "text_elements": []}]
        assert "developerInstructions" not in params


def test_app_server_server_requests_are_answered_for_daemon_mode(tmp_path: Path) -> None:
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    backend = RecordingAppServerBackend(config)

    async def run() -> None:
        backend._handle_message({"id": 1, "method": "item/commandExecution/requestApproval", "params": {}})
        backend._handle_message({"id": 2, "method": "item/tool/requestUserInput", "params": {}})
        backend._handle_message({"id": 3, "method": "item/tool/call", "params": {}})
        await asyncio.sleep(0)

    asyncio.run(run())

    assert backend.responses == [
        {"id": 1, "result": {"decision": "decline"}},
        {"id": 2, "result": {"answers": {}}},
        {
            "id": 3,
            "result": {
                "success": False,
                "contentItems": [{"type": "inputText", "text": "Dynamic tools are not available in Nyanpasu."}],
            },
        },
    ]


class RecordingAppServerBackend(CodexAppServerBackend):
    def __init__(self, config: NyanpasuConfig) -> None:
        super().__init__(config)
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[dict[str, Any]] = []

    async def _ensure_started(self) -> None:
        return None

    async def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        assert params is not None
        self.requests.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(f"unexpected request: {method}")

    async def _write(self, message: dict[str, Any]) -> None:
        self.responses.append(message)


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_during_start", [False, True])
async def test_cancel_waits_for_native_turn_to_stop(tmp_path, cancel_during_start):
    started, release_start, interrupted = (asyncio.Event() for _ in range(3))

    class Server(RecordingAppServerBackend):
        async def _request(self, method, params):
            if method == "turn/start":
                started.set()
                await release_start.wait()
            if method == "turn/interrupt":
                assert params == {"threadId": "thread-1", "turnId": "turn-1"}
                interrupted.set()
                return {}
            return await super()._request(method, params)

    backend = Server(NyanpasuConfig(state_dir=tmp_path))
    execution = asyncio.create_task(backend.run_turn(cwd=tmp_path, prompt="work", thread_id=None))
    await started.wait()
    if not cancel_during_start:
        release_start.set()
        while not backend._turn_waiters:
            await asyncio.sleep(0)
    execution.cancel()
    release_start.set()
    await asyncio.wait_for(interrupted.wait(), 1)
    assert not execution.done()  # An interrupt RPC acknowledgement alone is insufficient.
    backend._handle_message(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted", "items": []}},
        }
    )
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert backend._turn_waiters == {}


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["shutdown", "cleanup", "cancel"])
@pytest.mark.parametrize("failure", ["rpc", "completion_timeout"])
async def test_interrupt_failure_preserves_recovery_and_blocks_cleanup(tmp_path, monkeypatch, stop, failure):
    from nyanpasu.agent import AgentService
    from nyanpasu.backends import Backend, Backends
    from nyanpasu.models import SubtaskRequest, TaskAction
    from nyanpasu.transcript.codex import CodexHistorySource
    from tests.test_agent import FakeCodex, FakeWorktrees, _config, _task, fake_backends
    from tests.test_subtask_runtime import wait_status

    wait_for = asyncio.wait_for

    async def short_interrupt_deadline(awaitable, timeout):
        return await wait_for(awaitable, 0.01 if timeout == 30 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_interrupt_deadline)

    class Server(RecordingAppServerBackend):
        closed = False

        async def _request(self, method, params):
            if method == "turn/interrupt":
                if failure == "rpc":
                    raise ConnectionError("interrupt connection lost")
                return {}  # Acknowledged, but no turn/completed notification follows.
            return await super()._request(method, params)

        async def close(self):
            if not self.closed:
                assert first.store.get_context_lease(target.context_key) is not None
                self.closed = True
            await super().close()

    config = _config(tmp_path)
    backend = Server(config)
    worktrees = FakeWorktrees(tmp_path / "worktrees")
    first = AgentService(
        config, worktrees=worktrees, backends=Backends(config, {"codex": Backend(backend, CodexHistorySource(backend))})
    )
    try:
        target = _task("parent")
        if stop == "cancel":
            # The parent controls a real app-server child without starting a second native turn.
            first.store.record_task(target)
            first.store.mark_task_running(target.task_id, None)
            first._admitted_roots.add(target.task_id)
            target = await first.create_subtask(target.task_id, SubtaskRequest(request_key="child", prompt="child"))
        else:
            await first.submit(target)
        async with asyncio.timeout(3):
            while not backend._turn_waiters:
                await asyncio.sleep(0.005)
        if stop == "shutdown":
            await first.shutdown()
            assert first.store.task_status("parent") == "queued"
        else:
            if stop == "cancel":
                with pytest.raises(RuntimeError, match="Cannot confirm Codex stopped"):
                    await first.control.dispatch("parent", "cancel", {"task_ids": [target.task_id]})
                assert first.store.context_scope(target.context_key).lifecycle == "closing"
                assert first.store.task_status(target.task_id) == "running"
            await first.submit(_task("cleanup").model_copy(update={"action": TaskAction.CLEANUP}))
            await wait_status(first, "cleanup", "failed")
            assert first.store.task_status(target.task_id) == "running"
            assert first.store.context_scope("demo:1").lifecycle == "closing"
            await first.submit(_task("cleanup-retry").model_copy(update={"action": TaskAction.CLEANUP}))
            await wait_status(first, "cleanup-retry", "failed")
            assert first.store.get_context_lease(target.context_key) is not None
            assert not first.store.try_acquire_context_lease(
                target.context_key, owner_id="another-service", task_id="resume", ttl_seconds=60
            )
        assert worktrees.removed == []
        assert first.store.task_run(target.task_id).thread_id == "thread-1"
    finally:
        await first.shutdown()

    recovered = FakeCodex()
    second = AgentService(config, worktrees=worktrees, backends=fake_backends(config, recovered))
    try:
        await second.startup()
        await asyncio.wait_for(asyncio.gather(*second._tasks), 3)
        if stop == "shutdown":
            assert second.store.task_status("parent") == "completed"
            assert [thread for _, thread in recovered.calls] == ["thread-1"]
        else:
            assert second.store.context_scope("demo:1").lifecycle == "closed"
            assert recovered.calls == []
            assert len(worktrees.removed) == 1
    finally:
        await second.shutdown()
