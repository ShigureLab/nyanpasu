from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from nyanpasu.transcript.database import RecordNotFound

if TYPE_CHECKING:
    import sqlite3

CHUNK_BYTES = 64 * 1024
PREVIEW_BYTES = 4096
# One policy at ingestion, shared by every read/export path. This is deliberately
# a documented set of credential patterns, not a promise to recognize all secrets.
SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b|(?i:Bearer\s+)[A-Za-z0-9._~+/-]{12,}"
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def put_content(conn: sqlite3.Connection, session_id: str, text: str, previous: str | None = None) -> str:
    data = text.encode("utf-8", errors="replace")
    stream, start = uuid.uuid4().hex, 0
    if previous:
        row = conn.execute(
            "SELECT stream, size FROM transcript_contents WHERE ref=? AND session_id=?", (previous, session_id)
        ).fetchone()
        if row:
            stream, start = row["stream"], row["size"]
    for offset in range(0, len(data), CHUNK_BYTES):
        conn.execute(
            "INSERT INTO transcript_chunks VALUES (?, ?, ?)",
            (stream, start + offset, data[offset : offset + CHUNK_BYTES]),
        )
    ref = "c_" + uuid.uuid4().hex
    conn.execute("INSERT INTO transcript_contents VALUES (?, ?, ?, ?)", (ref, session_id, stream, start + len(data)))
    return ref


def content_bytes(
    conn: sqlite3.Connection, session_id: str, ref: str, offset: int = 0, limit: int = CHUNK_BYTES
) -> tuple[bytes, int]:
    row = conn.execute(
        "SELECT stream, size FROM transcript_contents WHERE ref=? AND session_id=?", (ref, session_id)
    ).fetchone()
    if row is None:
        raise RecordNotFound("Content does not exist in this session")
    size = row["size"]
    end = min(size, offset + limit)
    parts = conn.execute(
        "SELECT offset, data FROM transcript_chunks WHERE stream=? AND offset<? AND offset+length(data)>? ORDER BY offset",
        (row["stream"], end, offset),
    )
    return b"".join(bytes(part["data"])[max(0, offset - part["offset"]) : end - part["offset"]] for part in parts), size


def content_page(
    conn: sqlite3.Connection, session_id: str, ref: str, offset: int, limit: int, *, tail: bool = False
) -> dict[str, Any]:
    if tail:
        _, size = content_bytes(conn, session_id, ref, 0, 0)
        offset = max(0, size - limit)
    data, size = content_bytes(conn, session_id, ref, offset, limit)
    if tail:
        while data and data[0] & 0xC0 == 0x80:
            data = data[1:]
            offset += 1
    if offset > size or (data and data[0] & 0xC0 == 0x80):
        raise ValueError("Offset must be a UTF-8 byte boundary within this content version")
    text = data.decode("utf-8", errors="ignore")
    consumed = len(text.encode("utf-8"))
    return {
        "content_ref": ref,
        "text": text,
        "offset": offset,
        "next_offset": offset + consumed if offset + consumed < size else None,
        "recorded_bytes": size,
    }


def block(
    conn: sqlite3.Connection,
    session_id: str,
    block_id: str,
    kind: str,
    text: str,
    previous: str | None = None,
    mime: str = "text/plain",
) -> dict[str, Any]:
    ref = put_content(conn, session_id, text, previous)
    head, size = content_bytes(conn, session_id, ref, 0, PREVIEW_BYTES)
    return {
        "block_id": block_id,
        "kind": kind,
        "mime_type": mime,
        "preview": head.decode("utf-8", errors="ignore"),
        "preview_truncated": size > PREVIEW_BYTES,
        "content_ref": ref,
        "recorded_bytes": size,
    }
