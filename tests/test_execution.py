from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from nyanpasu.execution import JsonProcessRunner
from nyanpasu.resources import ProcessResources

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


@pytest.mark.anyio
async def test_runner_tracks_real_process_tree_resources_and_clears_them_on_cancellation(tmp_path):
    child = """
import json, os, time
buffer = bytearray(32 * 1024 * 1024)
print(json.dumps({"type": "ready", "pid": os.getpid()}), flush=True)
while True:
    until = time.process_time() + 0.2
    while time.process_time() < until:
        pass
    print('{"type": "tick"}', flush=True)
"""
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]).wait()"
    events = asyncio.Queue()
    runner = JsonProcessRunner()
    running = asyncio.create_task(
        runner.run(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            env=dict(os.environ),
            input_text="",
            timeout=10,
            received=events.put,
        )
    )
    try:
        ready = await asyncio.wait_for(events.get(), 5)
        assert ready["type"] == "ready"
        pids = runner.runtime_info()["pids"]
        assert len(pids) == 1 and pids[0] != ready["pid"]
        sampler = ProcessResources()
        first = sampler.sample({"claude": pids})["claude"]
        assert first["process_count"] == 2
        assert first["rss_bytes"] >= 32 * 1024 * 1024
        assert first["cpu_percent"] is None
        assert (await asyncio.wait_for(events.get(), 5))["type"] == "tick"
        second = sampler.sample({"claude": pids})["claude"]
        assert second["cpu_percent"] > 0
    finally:
        await runner.close()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert runner.runtime_info()["pids"] == []
    assert runner.runtime_info()["connection"] == "idle"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "posix", reason="Detached POSIX sessions are specific to POSIX backends")
@pytest.mark.parametrize("ignore_termination", [False, True])
async def test_runner_close_stops_detached_tools_before_returning(tmp_path, ignore_termination):
    child = f"""
import pathlib, signal, time
if {ignore_termination!r}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path('ready').touch()
while not pathlib.Path('after-close').exists():
    time.sleep(0.01)
pathlib.Path('late-write').write_text('old tool still running')
time.sleep(30)
"""
    parent = f"""
import json, pathlib, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, '-c', {child!r}], start_new_session=True,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
while not pathlib.Path('ready').exists():
    time.sleep(0.01)
print(json.dumps({{'type': 'ready', 'pid': child.pid}}), flush=True)
child.wait()
"""
    events = asyncio.Queue()
    runner = JsonProcessRunner()
    running = asyncio.create_task(
        runner.run(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            env=dict(os.environ),
            input_text="",
            timeout=10,
            received=events.put,
        )
    )
    tool = None
    try:
        ready = await asyncio.wait_for(events.get(), 5)
        tool = psutil.Process(ready["pid"])
        assert os.getpgid(tool.pid) == tool.pid
        assert tool.pid != runner.runtime_info()["pids"][0]
        await runner.close()
        (tmp_path / "after-close").touch()
        await asyncio.sleep(0.1)
        assert not (tmp_path / "late-write").exists()
        assert not tool.is_running() or tool.status() == psutil.STATUS_ZOMBIE
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        if tool is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                tool.kill()
        await runner.close()
        with contextlib.suppress(asyncio.CancelledError):
            await running
