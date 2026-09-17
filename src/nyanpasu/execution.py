from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from nyanpasu.diagnostics import Diagnostic, diagnostic
from nyanpasu.transcript.content import redact

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.models import RunResult

ExecutionStarted = Callable[[str, str | None], Awaitable[None]]


class ExecutionBackend(Protocol):
    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult: ...

    async def cleanup_thread(self, thread_id: str) -> None: ...

    async def close(self) -> None: ...

    def runtime_info(self) -> dict: ...


class JsonProcessRunner:
    def __init__(self):
        self.diagnostics: deque[Diagnostic] = deque(maxlen=100)
        self._runs: set[asyncio.Task] = set()

    def runtime_info(self) -> dict:
        return {"connection": "running" if self._runs else "idle", "diagnostics": list(self.diagnostics)}

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str,
        timeout: float,
        received: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[int, str]:
        async def receive(event: dict) -> None:
            if event.get("type", "").startswith("nyanpasu."):
                self.diagnostics.append(diagnostic(str(redact(event))))
            await received(event)

        task = asyncio.create_task(
            run_json_process(
                argv,
                cwd=cwd,
                env=env,
                input_text=input_text,
                timeout=timeout,
                received=receive,
                diagnostic=lambda text: self.diagnostics.append(diagnostic(text[-8192:])),
            )
        )
        self._runs.add(task)
        try:
            return await task
        finally:
            self._runs.discard(task)

    async def close(self) -> None:
        runs = tuple(self._runs)
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)


async def stop_process(proc: asyncio.subprocess.Process) -> None:
    """Allow the CLI to close its tools and journal before forcing a shutdown."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    elif proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        if os.name != "posix":
            proc.kill()
    finally:
        # A wrapper can exit while its children still own the output pipes.
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def run_json_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_text: str,
    timeout: float,
    received: Callable[[dict[str, Any]], Awaitable[None]],
    diagnostic: Callable[[str], None],
) -> tuple[int, str]:
    """Run JSONL output with stderr redacted before diagnostics or tail truncation."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stderr_tail = ""

    async def read_stdout() -> None:
        assert proc.stdout is not None
        async for event in json_lines(proc.stdout):
            await received(event)

    async def read_stderr() -> None:
        assert proc.stderr is not None
        maximum = 64 * 1024

        def emit(raw: bytes) -> None:
            nonlocal stderr_tail
            value = (
                redact(raw.decode("utf-8", "replace"))
                if len(raw) <= maximum
                else "stderr line exceeded 64 KiB; content omitted."
            )
            stderr_tail = (stderr_tail + value + "\n")[-8192:]
            diagnostic(value)

        pending = b""
        oversized = False
        while chunk := await proc.stderr.read(maximum):
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                if not oversized:
                    emit(line)
                oversized = False
            if len(pending) > maximum:
                if not oversized:
                    emit(pending)
                pending = b""
                oversized = True
        if pending and not oversized:
            emit(pending)

    async def communicate() -> None:
        assert proc.stdin is not None
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(read_stdout())
            tasks.create_task(read_stderr())
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(input_text.encode("utf-8"))
                await proc.stdin.drain()
            proc.stdin.close()
            await proc.wait()

    try:
        await asyncio.wait_for(communicate(), timeout)
        assert proc.returncode is not None
        return proc.returncode, stderr_tail.strip()
    finally:
        await stop_process(proc)


async def json_lines(stream: asyncio.StreamReader):
    """Read bounded JSONL, retaining malformed/oversized lines as visible records."""
    pending = bytearray()
    oversized = False
    maximum = 16 * 1024 * 1024
    while chunk := await stream.read(65536):
        pending.extend(chunk)
        while b"\n" in pending:
            raw, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            if oversized:
                yield {"type": "nyanpasu.invalid_json", "text": raw.decode("utf-8", "replace")}
                oversized = False
            elif raw.strip():
                yield parse_json_line(bytes(raw))
        if len(pending) > maximum:
            yield {
                "type": "nyanpasu.capture_gap",
                "text": "JSON line exceeded 16 MiB; raw fragments retained, semantic parsing unavailable.",
            }
            yield {"type": "nyanpasu.invalid_json", "text": pending.decode("utf-8", "replace")}
            pending.clear()
            oversized = True
    if pending:
        yield (
            parse_json_line(bytes(pending))
            if not oversized
            else {"type": "nyanpasu.invalid_json", "text": pending.decode("utf-8", "replace")}
        )


def parse_json_line(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value
    except (ValueError, UnicodeDecodeError) as exc:
        return {"type": "nyanpasu.invalid_json", "text": raw.decode("utf-8", "replace"), "error": str(exc)}
