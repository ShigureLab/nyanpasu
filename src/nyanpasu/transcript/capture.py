from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from anyio import to_thread
from loguru import logger

from nyanpasu.models import json_dumps

if TYPE_CHECKING:
    from nyanpasu.transcript.store import TranscriptStore

EventObserver = Callable[[dict[str, Any], str], None]
MAX_PENDING_BYTES = 16 * 1024 * 1024


class Capture:
    """A bounded, ordered writer; callbacks never block the shared RPC reader."""

    def __init__(self, store: TranscriptStore, task_id: str):
        self.store = store
        self.task_id = task_id
        self.queue: asyncio.Queue[tuple[dict[str, Any], str, int] | None] = asyncio.Queue(maxsize=512)
        self.pending_bytes = 0
        self.dropped = 0
        self.error: str | None = None
        self.worker = asyncio.create_task(self._write_loop())

    def observe(self, event: dict[str, Any], direction: str = "notification") -> None:
        size = len(json_dumps(event).encode())
        if self.queue.full() or self.pending_bytes + size > MAX_PENDING_BYTES:
            self.dropped += 1
            return
        self.pending_bytes += size
        self.queue.put_nowait((event, direction, size))

    async def flush(self) -> None:
        completed = asyncio.create_task(self.queue.join())
        done, _ = await asyncio.wait({completed, self.worker}, return_when=asyncio.FIRST_COMPLETED)
        if self.worker in done:
            completed.cancel()
            await self.worker
        await completed
        if self.dropped:
            await self._write_gap()

    async def close(self) -> None:
        await self.flush()
        await self.queue.put(None)
        await self.worker

    async def _write_loop(self) -> None:
        while (item := await self.queue.get()) is not None:
            event, direction, size = item
            try:
                await self._persist(event, direction)
                if self.dropped:
                    await self._write_gap()
            finally:
                self.pending_bytes -= size
                self.queue.task_done()

    async def _persist(self, event: dict[str, Any], direction: str) -> None:
        try:
            await to_thread.run_sync(functools.partial(self.store.record, self.task_id, event, direction))
        except (sqlite3.Error, OSError) as exc:
            self.error = str(exc)
            self.dropped += 1
            logger.error("Transcript persistence failed for task {}: {}", self.task_id, exc)

    async def _write_gap(self) -> None:
        count, self.dropped = self.dropped, 0
        await self._persist(
            {
                "type": "nyanpasu.capture_gap",
                "text": f"{count} observations were not persisted (writer backlog or storage failure); affected range unknown.",
            },
            "lifecycle",
        )
