from __future__ import annotations

import asyncio
import tomllib
from typing import TYPE_CHECKING, Any

import pytest

from nyanpasu.codex import CodexAppServerBackend, CodexExecBackend, safe_codex_env
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


def test_exec_backend_argv_includes_approvals_reviewer(tmp_path: Path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        codex=CodexConfig(approval_policy="on-request", approvals_reviewer="auto_review"),
    )
    backend = CodexExecBackend(config)

    argv = backend._argv(cwd=tmp_path, thread_id=None, output_path=tmp_path / "out.txt")

    assert "-c" in argv
    assert 'approvals_reviewer="auto_review"' in argv
    assert 'approval_policy="on-request"' in argv
    assert 'sandbox_mode="workspace-write"' in argv


def test_exec_instructions_are_toml_safe_for_new_and_resumed_sessions(tmp_path: Path) -> None:
    backend = CodexExecBackend(NyanpasuConfig(state_dir=tmp_path / "state"))
    instructions = 'Review "carefully".\n保留规则。 Path: C:\\repo; literal $(command) and `text`.'
    for thread_id in (None, "thread-1"):
        argv = backend._argv(
            cwd=tmp_path,
            thread_id=thread_id,
            output_path=tmp_path / "out.txt",
            developer_instructions=instructions,
        )
        override = next(arg for arg in argv if arg.startswith("developer_instructions="))
        assert tomllib.loads(override)["developer_instructions"] == instructions
        assert not any("base_instructions" in arg or "model_instructions_file" in arg for arg in argv)


@pytest.mark.parametrize("thread_id", [None, "thread-1"])
def test_exec_pins_model_and_effort_for_new_and_resumed_sessions(tmp_path: Path, thread_id) -> None:
    backend = CodexExecBackend(
        NyanpasuConfig(state_dir=tmp_path, codex=CodexConfig(model="configured-model", reasoning_effort="medium"))
    )

    argv = backend._argv(cwd=tmp_path, thread_id=thread_id, output_path=tmp_path / "out.txt")

    assert argv[argv.index("--model") + 1] == "configured-model"
    override = next(arg for arg in argv if arg.startswith("model_reasoning_effort="))
    assert tomllib.loads(override)["model_reasoning_effort"] == "medium"


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
@pytest.mark.parametrize("transport", ["exec", "app-server"])
async def test_custom_wrapper_runs_resumes_and_reads_history(tmp_path: Path, transport):
    import json
    import sys

    from nyanpasu.backends import Backends

    wrapper = tmp_path / "codex wrapper.py"
    log = tmp_path / "argv.jsonl"
    wrapper.write_text("""import json,os,sys
from pathlib import Path
assert sys.argv[1] == 'literal $(touch SHOULD_NOT_EXIST)'
with Path(os.environ['ARGV_LOG']).open('a') as log: log.write(json.dumps(sys.argv)+'\\n')
def send(value): print(json.dumps(value),flush=True)
item={'id':'answer','type':'agentMessage','text':'wrapper answer'}
turn={'id':'wrapper-turn','status':'completed','items':[item]}
if sys.argv[2]=='exec':
    sys.stdin.read()
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('wrapper answer')
    send({'type':'thread.started','thread_id':'wrapper-thread'})
    send({'type':'turn.started','turn_id':'wrapper-turn'})
else:
    assert sys.argv[2]=='app-server'
    for line in sys.stdin:
        event=json.loads(line)
        if 'id' not in event: continue
        method=event['method']
        result={}
        if method.startswith('thread/') and method not in ('thread/archive','thread/turns/list'):
            result={'thread':{'id':'wrapper-thread'}}
        elif method=='turn/start': result={'turn':turn}
        elif method=='thread/turns/list': result={'data':[turn],'nextCursor':None}
        send({'id':event['id'],'result':result})
        if method=='turn/start': send({'method':'turn/completed','params':{'threadId':'wrapper-thread','turn':turn}})
""")
    runtimes = Backends(
        NyanpasuConfig(
            state_dir=tmp_path,
            codex=CodexConfig(
                backend=transport,
                bin=sys.executable,
                args=(str(wrapper), "literal $(touch SHOULD_NOT_EXIST)"),
                env={"ARGV_LOG": str(log)},
            ),
        )
    )
    backend = runtimes.get("codex")
    try:
        first = await backend.execution.run_turn(cwd=tmp_path, prompt="first", thread_id=None)
        second = await backend.execution.run_turn(cwd=tmp_path, prompt="second", thread_id=first.thread_id)
        assert first.final_message == second.final_message == "wrapper answer"
        history = await backend.history.read_session(first.thread_id)
        assert history.metadata.backend == "codex"
        assert history.turns[0].items[0].presentation.blocks[0].text == "wrapper answer"
        await backend.execution.cleanup_thread(first.thread_id)
    finally:
        await runtimes.close()
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[-1][2] == "app-server"
    assert len(commands) == (3 if transport == "exec" else 1)
    if transport == "exec":
        assert commands[1][2:5] == ["exec", "resume", "wrapper-thread"]
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
