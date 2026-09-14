from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nyanpasu.transcript.content import PREVIEW_BYTES, content_ref, fingerprint
from nyanpasu.transcript.history import SessionHistory, SessionMetadata, session_key
from nyanpasu.transcript.models import Coverage, Source, TranscriptBlock, TranscriptEntry

if TYPE_CHECKING:
    from nyanpasu.transcript.history import SessionSource


class RecordNotFound(KeyError):
    pass


class SourceUnavailable(RuntimeError):
    pass


def iso_time(timestamp: float | None = None) -> str:
    value = datetime.now(UTC) if timestamp is None else datetime.fromtimestamp(timestamp, UTC)
    return value.isoformat()


@dataclass(frozen=True)
class Snapshot:
    """A request-local view of native history. Nothing here is written to disk."""

    metadata: SessionMetadata
    turns: tuple[dict[str, Any], ...]
    entries: tuple[TranscriptEntry, ...]
    contents: dict[str, str]
    read_at: str

    @property
    def session_id(self) -> str:
        return session_key(self.metadata.backend, self.metadata.id)

    @property
    def generation(self) -> str:
        return fingerprint([self.session_id, self.metadata.created_at])

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
        raise RecordNotFound("Item not found in this session")

    def position(self, entry_id: str) -> int:
        return int(self.entry(entry_id).first_seq) - 1


def make_snapshot(history: SessionHistory, task_by_turn: dict[str, str], read_at: str) -> Snapshot:
    entries: list[TranscriptEntry] = []
    contents: dict[str, str] = {}
    session_id = session_key(history.metadata.backend, history.metadata.id)
    for turn in history.turns:
        for item in turn.items:
            update = item.presentation
            timing = {key: getattr(item, key) for key in ("started_at", "completed_at", "recorded_at")}
            entry = TranscriptEntry(
                entry_id=item.id,
                session_id=session_id,
                thread_id=history.metadata.id,
                task_id=task_by_turn.get(turn.id),
                turn_id=turn.id,
                first_seq=str(len(entries) + 1),
                revision_seq=fingerprint(item.model_dump()),
                kind=update.kind or "unknown",
                title=update.title or "Session item",
                observed_at=read_at,
                started_at=timing.get("started_at"),
                completed_at=timing.get("completed_at"),
                recorded_at=timing.get("recorded_at"),
                source=Source(backend=history.metadata.backend, version=history.metadata.cli_version, origin="native"),
                coverage=Coverage(redacted=item.redacted, source_truncated=update.source_truncated),
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
    turns = tuple({"id": turn.id, "items": [item.raw for item in turn.items]} for turn in history.turns)
    return Snapshot(history.metadata, turns, tuple(entries), contents, read_at)


async def read_snapshot(source: SessionSource, thread_id: str, task_by_turn: dict[str, str]) -> Snapshot:
    read_at = iso_time()
    try:
        history = await source.read_session(thread_id)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        raise SourceUnavailable(f"Cannot read this session: {exc}") from exc
    return make_snapshot(history, task_by_turn, read_at)
