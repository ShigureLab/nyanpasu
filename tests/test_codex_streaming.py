from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest

from nyanpasu.codex import CodexAppServerBackend, CodexExecBackend, json_lines
from nyanpasu.config import CodexConfig, NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_exec_binds_thread_before_exit_and_reports_failed_output(tmp_path: Path):
    program = tmp_path / "codex"
    program.write_text(f"""#!{sys.executable}
import sys,time,json
sys.stdin.read()
print(json.dumps({{"type":"thread.started","thread_id":"live-thread"}}),flush=True)
print(json.dumps({{"type":"item.started","item":{{"id":"tool","type":"command_execution","command":"controlled failure","aggregated_output":"visible before exit","status":"in_progress"}}}}),flush=True)
sys.stderr.write("diagnostic\\n" * 10000);sys.stderr.flush()
time.sleep(0.8)
sys.exit(7)
""")
    program.chmod(0o755)
    config = NyanpasuConfig(state_dir=tmp_path, codex=CodexConfig(bin=str(program), backend="exec"))
    state = StateStore(config.db_path)
    task = AgentTask(task_id="live-task", context_key="live", action=TaskAction.RUN, prompt="go")
    state.record_task(task)
    backend = CodexExecBackend(config)
    started = asyncio.Event()

    async def on_started(thread_id: str, turn_id: str | None):
        state.bind_task_execution(task.task_id, thread_id, turn_id)
        started.set()

    runner = asyncio.create_task(backend.run_turn(cwd=tmp_path, prompt="go", thread_id=None, on_started=on_started))
    await asyncio.wait_for(started.wait(), 1)
    assert not runner.done()
    assert state.recent_tasks()[0].thread_id == "live-thread"
    with pytest.raises(RuntimeError, match="diagnostic"):
        await runner
    await backend.close()


@pytest.mark.anyio
async def test_jsonl_handles_split_unicode_bad_lines_and_missing_newline():
    stream = asyncio.StreamReader()
    payload = '{"text":"你好🙂"}\nnot-json\n{"last":true}'.encode()

    async def feed():
        for index in range(0, len(payload), 3):
            stream.feed_data(payload[index : index + 3])
            await asyncio.sleep(0)
        stream.feed_eof()

    feeder = asyncio.create_task(feed())
    events = [event async for event in json_lines(stream)]
    await feeder
    assert events[0] == {"text": "你好🙂"}
    assert events[1]["type"] == "nyanpasu.invalid_json"
    assert events[2] == {"last": True}


@pytest.mark.anyio
async def test_app_server_streams_and_reports_eof_without_waiting_for_timeout(tmp_path: Path):
    program = tmp_path / "app-server"
    program.write_text(f"""#!{sys.executable}
import sys,json,time
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    method=request.get("method")
    if method=="initialize": send({{"id":request["id"],"result":{{}}}})
    elif method=="thread/start": send({{"id":request["id"],"result":{{"thread":{{"id":"thread"}}}}}})
    elif method=="turn/start":
        send({{"id":request["id"],"result":{{"turn":{{"id":"turn"}}}}}})
        send({{"method":"item/started","params":{{"threadId":"thread","turnId":"turn","item":{{"id":"item","type":"agentMessage","text":"before crash"}}}}}})
        send({{"id":900,"method":"item/commandExecution/requestApproval","params":{{"threadId":"thread","turnId":"turn","itemId":"command"}}}})
        response=json.loads(sys.stdin.readline())
        assert response=={{"id":900,"result":{{"decision":"decline"}}}}
        sys.stderr.write("stderr flood\\n"*30000);sys.stderr.flush()
        time.sleep(0.3)
        sys.exit(2)
""")
    program.chmod(0o755)
    backend = CodexAppServerBackend(NyanpasuConfig(state_dir=tmp_path, codex=CodexConfig(bin=str(program))))
    started = asyncio.Event()
    bindings = []

    async def on_started(thread_id: str, turn_id: str | None):
        bindings.append((thread_id, turn_id))
        if turn_id:
            started.set()

    runner = asyncio.create_task(
        backend.run_turn(
            cwd=tmp_path,
            prompt="test",
            thread_id=None,
            on_started=on_started,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not runner.done()
        assert bindings == [("thread", None), ("thread", "turn")]
        with pytest.raises(RuntimeError, match="closed its output"):
            await asyncio.wait_for(runner, 3)
        assert backend.diagnostics
    finally:
        await backend.close()
