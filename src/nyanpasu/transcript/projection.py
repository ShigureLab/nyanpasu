from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from nyanpasu.models import json_dumps
from nyanpasu.transcript.content import block
from nyanpasu.transcript.models import ContentUpdate, Coverage, EntryUpdate, Source, TranscriptBlock, TranscriptEntry

if TYPE_CHECKING:
    import sqlite3

TERMINAL_STATES = {"completed", "failed", "declined", "interrupted", "responded"}


def project_entry(
    conn: sqlite3.Connection,
    *,
    session: sqlite3.Row,
    task_id: str,
    update: EntryUpdate,
    seq: int,
    timestamp: str,
    source: Source,
    thread_id: str | None,
    turn_id: str | None,
    redacted: bool,
) -> TranscriptEntry:
    source_key = update.key or f"event:{seq}"
    row = conn.execute(
        """SELECT data FROM transcript_entries
           WHERE session_id=? AND task_id=? AND source_key=?""",
        (session["session_id"], task_id, source_key),
    ).fetchone()
    entry = (
        TranscriptEntry.model_validate_json(row["data"])
        if row
        else TranscriptEntry(
            entry_id="e_" + uuid.uuid4().hex,
            session_id=session["session_id"],
            task_id=task_id,
            first_seq=str(seq),
            revision_seq=str(seq),
            kind=update.kind or "unknown",
            title=update.title or update.kind or "Unknown event",
            observed_at=timestamp,
            source=source,
            state=update.state or ("running" if update.source_item_id else "recorded"),
        )
    )
    entry = entry.model_copy(update=update.fields())
    entry.revision_seq = str(seq)
    entry.thread_id = thread_id
    entry.turn_id = turn_id
    entry.raw_event_count += 1
    entry.coverage.source_truncated |= update.source_truncated
    entry.coverage.redacted |= redacted
    if update.missing_parts:
        entry.coverage.capture_gap = True
        entry.coverage.missing_parts = list(dict.fromkeys([*entry.coverage.missing_parts, *update.missing_parts]))
    if entry.state == "running" and entry.started_at is None:
        entry.started_at = timestamp
    if entry.state in TERMINAL_STATES and entry.ended_at is None:
        entry.ended_at = timestamp
    update_blocks(conn, entry, update.blocks)
    save_entry(conn, entry, source_key)
    return entry


def update_blocks(conn: sqlite3.Connection, entry: TranscriptEntry, updates: list[ContentUpdate]) -> None:
    updates = list(updates)
    if entry.command and len(entry.command) > 2048:
        updates.append(ContentUpdate(block_id="command", kind="text", text=entry.command))
        entry.command = entry.command[:2048] + "…"
    blocks = {item.block_id: item for item in entry.blocks}
    for change in updates:
        existing = blocks.get(change.block_id)
        previous = existing.content_ref if existing and change.append else None
        value = block(
            conn,
            entry.session_id,
            change.block_id,
            change.kind,
            change.text,
            previous,
            "application/json" if change.kind == "json" else "text/plain",
        )
        blocks[change.block_id] = TranscriptBlock.model_validate(value)
    entry.blocks = list(blocks.values())


def save_entry(conn: sqlite3.Connection, entry: TranscriptEntry, source_key: str) -> None:
    encoded = entry.model_dump_json()
    conn.execute(
        """INSERT INTO transcript_entries VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(entry_id) DO UPDATE SET revision_seq=excluded.revision_seq, data=excluded.data""",
        (
            entry.entry_id,
            entry.session_id,
            entry.task_id,
            source_key,
            int(entry.first_seq),
            int(entry.revision_seq),
            encoded,
        ),
    )
    conn.execute(
        "INSERT INTO transcript_changes VALUES (?,?,?,?)",
        (int(entry.revision_seq), entry.session_id, entry.entry_id, encoded),
    )
    conn.execute(
        "UPDATE transcript_events SET entry_id=? WHERE seq=?",
        (entry.entry_id, int(entry.revision_seq)),
    )


def merge_coverage(conn: sqlite3.Connection, session: sqlite3.Row, update: EntryUpdate, *, redacted: bool) -> None:
    if not update.missing_parts and not redacted:
        return
    coverage = Coverage.model_validate_json(session["coverage"])
    coverage.capture_gap |= bool(update.missing_parts)
    coverage.redacted |= redacted
    coverage.missing_parts = list(dict.fromkeys([*coverage.missing_parts, *update.missing_parts]))
    conn.execute(
        "UPDATE transcript_sessions SET coverage=? WHERE session_id=?",
        (json_dumps(coverage.model_dump()), session["session_id"]),
    )
