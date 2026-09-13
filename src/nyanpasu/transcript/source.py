from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.to_thread as to_thread

from nyanpasu.transcript.adapters import item_snapshot
from nyanpasu.transcript.content import PREVIEW_BYTES, content_ref, fingerprint, redact
from nyanpasu.transcript.models import Coverage, Source, TranscriptBlock, TranscriptEntry

if TYPE_CHECKING:
    from nyanpasu.codex import CodexSessionSource


class RecordNotFound(KeyError):
    pass


class SourceUnavailable(RuntimeError):
    pass


def iso_time(timestamp: float | None = None) -> str:
    value = datetime.now(UTC) if timestamp is None else datetime.fromtimestamp(timestamp, UTC)
    return value.isoformat()


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


@dataclass(frozen=True)
class Snapshot:
    """A request-local view of Codex data. Nothing here is written to disk."""

    thread: dict[str, Any]
    turns: tuple[dict[str, Any], ...]
    entries: tuple[TranscriptEntry, ...]
    contents: dict[str, str]
    read_at: str

    @property
    def session_id(self) -> str:
        return self.thread["id"]

    @property
    def generation(self) -> str:
        return fingerprint([self.session_id, self.thread.get("createdAt")])

    @property
    def coverage(self) -> dict[str, Any]:
        return Coverage(
            redacted=any(entry.coverage.redacted for entry in self.entries),
            source_truncated=any(entry.coverage.source_truncated for entry in self.entries),
        ).model_dump()

    def entry(self, entry_id: str) -> TranscriptEntry:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        raise RecordNotFound("Item not found in this Codex session")

    def position(self, entry_id: str) -> int:
        return int(self.entry(entry_id).first_seq) - 1


def make_snapshot(
    thread: dict[str, Any],
    turns: list[dict[str, Any]],
    task_by_turn: dict[str, str],
    read_at: str,
    times: dict[tuple[str, str], dict[str, str | None]],
) -> Snapshot:
    entries: list[TranscriptEntry] = []
    contents: dict[str, str] = {}
    session_id = thread["id"]
    for turn in turns:
        for original in turn["items"]:
            item = redact(original)
            update = item_snapshot(item)
            timing = times.get((turn["id"], item["id"]), {})
            entry = TranscriptEntry(
                entry_id=item["id"],
                session_id=session_id,
                thread_id=session_id,
                task_id=task_by_turn.get(turn["id"]),
                turn_id=turn["id"],
                first_seq=str(len(entries) + 1),
                revision_seq=fingerprint([item, timing]),
                kind=update.kind or "unknown",
                title=update.title or "Codex item",
                observed_at=read_at,
                started_at=timing.get("started_at"),
                completed_at=timing.get("completed_at"),
                recorded_at=timing.get("recorded_at"),
                source=Source(backend="codex", version=thread.get("cliVersion"), origin="codex"),
                coverage=Coverage(redacted=item != original, source_truncated=update.source_truncated),
            ).model_copy(update=update.fields())
            for change in update.blocks:
                ref = content_ref(session_id, entry.entry_id, change.block_id, change.text)
                contents[ref] = change.text
                data = change.text.encode("utf-8")
                entry.blocks.append(
                    TranscriptBlock(
                        block_id=change.block_id,
                        kind=change.kind,
                        mime_type="application/json" if change.kind == "json" else "text/plain",
                        preview=data[:PREVIEW_BYTES].decode("utf-8", errors="ignore"),
                        preview_truncated=len(data) > PREVIEW_BYTES,
                        content_ref=ref,
                        recorded_bytes=len(data),
                    )
                )
            entries.append(entry)
    return Snapshot(redact(thread), tuple(redact(turns)), tuple(entries), contents, read_at)


async def read_snapshot(source: CodexSessionSource, thread_id: str, task_by_turn: dict[str, str]) -> Snapshot:
    read_at = iso_time()
    try:
        thread = await source.read_thread(thread_id)
        turns: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = await source.list_turns(thread_id, cursor)
            turns.extend(page["data"])
            cursor = page.get("nextCursor")
            if cursor is None:
                break
        times = await to_thread.run_sync(item_times, thread)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise SourceUnavailable(f"Cannot read this session from Codex: {exc}") from exc
    return make_snapshot(thread, turns, task_by_turn, read_at, times)
