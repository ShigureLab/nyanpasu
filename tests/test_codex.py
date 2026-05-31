from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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
        backend._thread_events["thread-1"] = []
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
        backend._thread_events["thread-1"] = [{"event": "stale"}]

        alive = loop.run_until_complete(backend._process_alive())

        assert alive is False
        assert backend._proc is None
        assert backend._reader_task is None
        assert backend._pending == {}
        assert backend._thread_events == {}
        assert waiter.done()
        assert isinstance(waiter.exception(), RuntimeError)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
