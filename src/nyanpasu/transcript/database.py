from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RecordNotFound(KeyError):
    pass


class TranscriptDatabase:
    def __init__(self, path: Path):
        self.db_path = path
        with self.connect(write=True) as conn:
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def session(self, conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM transcript_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise RecordNotFound("Session not found")
        return row
