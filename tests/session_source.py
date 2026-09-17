from __future__ import annotations

from copy import deepcopy
from typing import Any

from nyanpasu.transcript.codex import CodexHistorySource


class MemorySessionSource:
    """A stand-in for the external Codex API; the application never writes it."""

    def __init__(self, turns: list[dict[str, Any]] | None = None, *, metadata: dict[str, Any] | None = None):
        self.turns = turns or []
        self.metadata = metadata or {}
        self.calls: list[tuple[str, str, str | None]] = []

    async def read_session(self, thread_id: str):
        return await CodexHistorySource(self).read_session(thread_id)

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(("read", thread_id, None))
        return {"id": thread_id, "createdAt": 1, "cliVersion": "test", **self.metadata}

    async def list_turns(self, thread_id: str, cursor: str | None = None) -> dict[str, Any]:
        self.calls.append(("turns", thread_id, cursor))
        offset = int(cursor or 0)
        return {
            "data": deepcopy(self.turns[offset : offset + 2]),
            "nextCursor": str(offset + 2) if offset + 2 < len(self.turns) else None,
        }


def tool(item_id: str, output: str, status: str = "completed") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "commandExecution",
        "command": "echo same",
        "aggregatedOutput": output,
        "status": status,
    }


def turn(turn_id: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {"id": turn_id, "items": list(items), "status": "completed"}
