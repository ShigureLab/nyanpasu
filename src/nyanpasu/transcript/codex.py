from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.to_thread as to_thread

from nyanpasu.transcript.adapters import item_snapshot
from nyanpasu.transcript.content import redact
from nyanpasu.transcript.history import HistoryItem, HistoryTurn, SessionHistory, SessionMetadata
from nyanpasu.transcript.source import iso_time

if TYPE_CHECKING:
    from nyanpasu.codex import CodexSessionSource


def item_times(thread: dict[str, Any]) -> dict[tuple[str, str], dict[str, str | None]]:
    """Read timing annotations from Codex's own rollout, without copying its journal."""
    path = thread.get("path")
    if path is None:
        return {}
    times = {}
    try:
        with Path(path).open(encoding="utf-8") as rollout:
            for line in rollout:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break  # Codex may still be appending the last record.
                    raise
                payload = record.get("payload", {})
                if record.get("type") != "event_msg" or payload.get("type") not in {"item_started", "item_completed"}:
                    continue
                if payload.get("thread_id") != thread["id"]:
                    continue
                started, completed = payload.get("started_at_ms"), payload.get("completed_at_ms")
                times[(payload["turn_id"], payload["item"]["id"])] = {
                    "started_at": iso_time(started / 1000) if started is not None else None,
                    "completed_at": iso_time(completed / 1000) if completed is not None else None,
                    "recorded_at": record["timestamp"],
                }
    except OSError:
        # Ephemeral sessions and a rollout being archived can have no readable path.
        # The UI marks unavailable times rather than substituting the page read time.
        return {}
    return times


class CodexHistorySource:
    def __init__(self, client: CodexSessionSource):
        self.client = client

    @staticmethod
    def _item(item: dict, times: dict) -> HistoryItem:
        safe = redact(item)
        return HistoryItem(id=item["id"], presentation=item_snapshot(safe), raw=safe, redacted=safe != item, **times)

    async def read_session(self, thread_id: str) -> SessionHistory:
        thread = await self.client.read_thread(thread_id)
        turns = []
        cursor = None
        while True:
            page = await self.client.list_turns(thread_id, cursor)
            turns.extend(page["data"])
            cursor = page.get("nextCursor")
            if cursor is None:
                break
        times = await to_thread.run_sync(item_times, thread)
        return SessionHistory(
            metadata=SessionMetadata(
                id=thread_id,
                backend="codex",
                cwd=thread.get("cwd"),
                model=thread.get("model"),
                provider=thread.get("modelProvider"),
                reasoning_effort=thread.get("reasoningEffort"),
                cli_version=thread.get("cliVersion"),
                created_at=iso_time(thread["createdAt"]) if thread.get("createdAt") is not None else None,
                updated_at=iso_time(thread["updatedAt"]) if thread.get("updatedAt") is not None else None,
            ),
            turns=tuple(
                HistoryTurn(
                    id=turn["id"],
                    items=tuple(self._item(item, times.get((turn["id"], item["id"]), {})) for item in turn["items"]),
                )
                for turn in turns
            ),
        )
