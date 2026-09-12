from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest

from nyanpasu.codex import CodexAppServerBackend, CodexExecBackend, json_lines
from nyanpasu.config import CodexConfig, NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.transcript import TranscriptStore
from nyanpasu.transcript.capture import Capture

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_exec_persists_before_exit_and_keeps_failed_output(tmp_path: Path):
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
    store = TranscriptStore(config.db_path)
    session = store.begin(task, None, "exec")
    capture = Capture(store, task.task_id)
    backend = CodexExecBackend(config)
    runner = asyncio.create_task(backend.run_turn(cwd=tmp_path, prompt="go", thread_id=None, observer=capture.observe))
    try:
        for _ in range(100):
            entries = store.reader.window(session)["entries"]
            if any(entry["kind"] == "tool" for entry in entries):
                break
            await asyncio.sleep(0.01)
        assert not runner.done()
        assert any("visible before exit" in block["preview"] for entry in entries for block in entry["blocks"])
        with pytest.raises(RuntimeError):
            await runner
    finally:
        await capture.close()
    saved = TranscriptStore(config.db_path).reader.window(session)["entries"]
    assert any(entry["title"] == "Backend stderr" for entry in saved)
    assert any(entry["title"] == "process_exit" for entry in saved)


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
async def test_app_server_records_request_response_and_isolates_threads(tmp_path: Path):
    backend = CodexAppServerBackend(NyanpasuConfig(state_dir=tmp_path))
    first, second = [], []
    backend._thread_observers["a"] = lambda event, direction: first.append((event, direction))
    backend._thread_observers["b"] = lambda event, direction: second.append((event, direction))
    backend._handle_message(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "b", "turnId": "tb", "itemId": "same", "delta": "B"},
        }
    )
    backend._handle_message({"method": "global/unknown", "params": {}})
    assert first == []
    assert second[0][0]["params"]["delta"] == "B"
    assert len(backend.diagnostics) == 1


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
    events = []
    runner = asyncio.create_task(
        backend.run_turn(
            cwd=tmp_path,
            prompt="test",
            thread_id=None,
            observer=lambda event, direction: events.append((event, direction)),
        )
    )
    try:
        for _ in range(100):
            if any(event.get("method") == "item/started" for event, _ in events):
                break
            await asyncio.sleep(0.01)
        assert not runner.done()
        assert any(direction == "client_request" for _, direction in events)
        with pytest.raises(RuntimeError, match="closed its output"):
            await asyncio.wait_for(runner, 3)
        assert any(direction == "server_request" and event["id"] == 900 for event, direction in events)
        assert any(
            direction == "client_response" and event["result"] == {"decision": "decline"} for event, direction in events
        )
        assert backend._thread_observers == {}
    finally:
        await backend.close()
