from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nyanpasu.transcript.adapters import display, item_snapshot
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


@dataclass(frozen=True)
class Snapshot:
    """A request-local view of Codex data. Nothing here is written to disk."""

    thread: dict[str, Any]
    turns: tuple[dict[str, Any], ...]
    entries: tuple[TranscriptEntry, ...]
    contents: dict[str, str]
    originals: dict[str, str]
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
    thread: dict[str, Any], turns: list[dict[str, Any]], task_by_turn: dict[str, str], read_at: str
) -> Snapshot:
    entries: list[TranscriptEntry] = []
    contents: dict[str, str] = {}
    originals: dict[str, str] = {}
    session_id = thread["id"]
    for turn in turns:
        for original in turn["items"]:
            item = redact(original)
            update = item_snapshot(item)
            entry = TranscriptEntry(
                entry_id=item["id"],
                session_id=session_id,
                thread_id=session_id,
                task_id=task_by_turn.get(turn["id"]),
                turn_id=turn["id"],
                first_seq=str(len(entries) + 1),
                revision_seq=fingerprint(item),
                kind=update.kind or "unknown",
                title=update.title or "Codex item",
                observed_at=read_at,
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
            raw = display({"turnId": turn["id"], "item": item})
            raw_ref = content_ref(session_id, entry.entry_id, "source", raw)
            contents[raw_ref] = raw
            originals[entry.entry_id] = raw_ref
            entries.append(entry)
    return Snapshot(redact(thread), tuple(redact(turns)), tuple(entries), contents, originals, read_at)


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
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise SourceUnavailable(f"Cannot read this session from Codex: {exc}") from exc
    return make_snapshot(thread, turns, task_by_turn, read_at)
