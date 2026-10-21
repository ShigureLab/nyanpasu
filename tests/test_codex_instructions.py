from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

import pytest

from nyanpasu.codex import CodexAppServerBackend
from nyanpasu.config import CodexBackendConfig, NyanpasuConfig, ProcessConfig
from nyanpasu.targets import ExecutionTarget

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_session", [False, True])
async def test_resumed_turn_delivers_current_instructions_to_model(tmp_path, legacy_session):
    """Exercise the real app-server against a local Responses endpoint, without credentials or inference."""
    binary = os.environ.get("NYANPASU_TEST_CODEX") or shutil.which("codex")
    if binary is None:
        pytest.skip("Install Codex or set NYANPASU_TEST_CODEX to test its prompt delivery")
    requests = []

    async def respond(reader, writer):
        headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        headers = dict(line.lower().split(": ", 1) for line in headers[1:] if ": " in line)
        body = await reader.readexactly(int(headers["content-length"]))
        if headers.get("content-encoding") == "zstd":
            body = zstd.decompress(body)
        requests.append(json.loads(body))
        message = {
            "type": "message",
            "id": "msg_fixture",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
        events = [
            {"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": message},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_fixture",
                    "status": "completed",
                    "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            },
        ]
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
        for event in events:
            writer.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        native_home = tmp_path / "codex"
        native_home.mkdir()
        overrides = {
            "model_provider": "fixture",
            "model_providers.fixture.name": "Fixture",
            "model_providers.fixture.base_url": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
            "model_providers.fixture.wire_api": "responses",
            "model_providers.fixture.requires_openai_auth": False,
            "model_providers.fixture.supports_websockets": False,
            "check_for_update_on_startup": False,
        }
        command = [binary]
        for key, value in overrides.items():
            command.extend(("-c", f"{key}={json.dumps(value)}"))
        config = NyanpasuConfig(
            state_dir=tmp_path,
            backends={
                "codex": CodexBackendConfig(
                    process=ProcessConfig(
                        command=tuple(command),
                        env={"CODEX_HOME": str(native_home), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                    )
                )
            },
        )
        execution = ExecutionTarget(
            backend="codex", driver="codex", model="gpt-6.1-sol", reasoning="low", turn_timeout_seconds=15
        )

        class LegacyBackend(CodexAppServerBackend):
            async def _request(self, method, params):
                # Reproduce persisted sessions created before per-turn context delivery.
                if method == "thread/start":
                    params["developerInstructions"] = instructions
                elif method == "turn/start":
                    params.pop("additionalContext", None)
                return await super()._request(method, params)

        thread_id = None
        for index in range(2):
            control = tmp_path / f"control-{index}.json"
            control.write_text("{}")
            instructions = f"Current task control file: {control}"
            backend_type = LegacyBackend if legacy_session and index == 0 else CodexAppServerBackend
            backend = backend_type(config)
            try:
                result = await backend.run_turn(
                    cwd=tmp_path,
                    prompt="Return done.",
                    thread_id=thread_id,
                    developer_instructions=instructions,
                    execution=execution,
                )
                assert result.final_message == "done"
                thread_id = result.thread_id
                developer_text = "\n".join(
                    part.get("text", "")
                    for item in requests[-1]["input"]
                    if item.get("role") == "developer"
                    for part in item.get("content", [])
                )
                assert instructions in developer_text
                if index:
                    assert not (tmp_path / "control-0.json").exists()
                    assert developer_text.rfind(str(control)) > developer_text.rfind(str(tmp_path / "control-0.json"))
            finally:
                await backend.close()
                control.unlink()
        assert len(requests) == 2
