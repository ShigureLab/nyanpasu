from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from nyanpasu.execution import JsonProcessRunner

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def capture_stderr(tmp_path: Path, monkeypatch):
    proc = Mock(
        stdout=Mock(read=AsyncMock(return_value=b"")),
        stdin=Mock(drain=AsyncMock()),
        wait=AsyncMock(return_value=7),
        returncode=7,
    )
    monkeypatch.setattr("nyanpasu.execution.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr("nyanpasu.execution.stop_process", AsyncMock())

    async def capture(chunks: list[bytes]):
        proc.stderr = Mock(read=AsyncMock(side_effect=[*chunks, b""]))
        runner = JsonProcessRunner()
        returncode, tail = await runner.run(
            ["agent"], cwd=tmp_path, env={}, input_text="", timeout=1, received=AsyncMock()
        )
        assert returncode == 7
        return tail, runner.runtime_info()["diagnostics"]

    return capture


@pytest.mark.anyio
@pytest.mark.parametrize("credential", ["ghp_" + "a" * 36, "sk-" + "b" * 36, "Bearer " + "c" * 36])
@pytest.mark.parametrize("ending", [b"\n", b""])
async def test_stderr_redacts_credentials_split_across_reads(capture_stderr, credential, ending):
    payload = ("请求失败: " + credential).encode() + ending
    tail, diagnostics = await capture_stderr([payload[index : index + 3] for index in range(0, len(payload), 3)])
    assert tail == "请求失败: [REDACTED]"
    assert [entry["message"] for entry in diagnostics] == [tail]


@pytest.mark.anyio
async def test_stderr_redacts_before_truncating_the_diagnostic_and_error_tail(capture_stderr):
    tail, diagnostics = await capture_stderr([b"ghp_" + b"a" * 9000 + b"\n"])
    assert tail == "[REDACTED]"
    assert [entry["message"] for entry in diagnostics] == [tail]


@pytest.mark.anyio
@pytest.mark.parametrize("chunk_size", [5000, 65536])
@pytest.mark.parametrize("ending", [b"\nrecovered\n", b""])
async def test_stderr_omits_oversized_lines_and_resumes_at_the_next_line(capture_stderr, chunk_size, ending):
    payload = b"ghp_" + b"a" * (150 * 1024) + ending
    tail, diagnostics = await capture_stderr(
        [payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)]
    )
    messages = ["stderr line exceeded 64 KiB; content omitted."]
    if ending:
        messages.append("recovered")
    assert [entry["message"] for entry in diagnostics] == messages
    assert tail == "\n".join(messages)


@pytest.mark.anyio
async def test_stderr_preserves_individual_log_lines_and_decodes_unfinished_utf8(capture_stderr):
    tail, diagnostics = await capture_stderr([b"2026-09-17T00:00:00Z WARN agent: retrying\r\nnext\nlast \xe4"])
    assert diagnostics[0] == {
        "timestamp": "2026-09-17T00:00:00Z",
        "level": "warn",
        "target": "agent",
        "message": "retrying",
    }
    assert [entry["message"] for entry in diagnostics[1:]] == ["next", "last �"]
    assert tail.endswith("\nnext\nlast �")
