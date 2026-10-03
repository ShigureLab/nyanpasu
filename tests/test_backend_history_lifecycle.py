from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from nyanpasu.backends import Backend, Backends
from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import NativeSessionLocation
from nyanpasu.transcript.history import SessionMetadata

if TYPE_CHECKING:
    from nyanpasu.execution import ExecutionBackend
    from nyanpasu.transcript.history import SessionSource


@pytest.mark.anyio
async def test_native_history_processes_are_bounded_and_closed_after_each_request(tmp_path, monkeypatch):
    backends = Backends(NyanpasuConfig(state_dir=tmp_path))
    backends.register_session_locator(
        lambda backend, thread: NativeSessionLocation(
            native_home=tmp_path / thread / ".codex", isolated_home=tmp_path / thread, driver="codex"
        )
    )
    active = maximum = closed = 0

    class HistoryProcess:
        async def read_metadata(self, thread_id):
            await asyncio.sleep(0)
            if thread_id == "failure":
                raise RuntimeError("native reader failed")
            return SessionMetadata(id=thread_id, backend="codex")

        async def close(self):
            nonlocal active, closed
            active -= 1
            closed += 1

    def create(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        process = HistoryProcess()
        return Backend(cast("ExecutionBackend", process), cast("SessionSource", process))

    monkeypatch.setattr(backends, "_create", create)
    source = backends.source("codex")
    results = await asyncio.gather(
        *(source.read_metadata(str(index)) for index in range(8)),
        source.read_metadata("failure"),
        return_exceptions=True,
    )
    assert maximum == 2
    assert active == 0 and closed == 9
    assert len([result for result in results if isinstance(result, RuntimeError)]) == 1
    assert len([result for result in results if isinstance(result, SessionMetadata)]) == 8
