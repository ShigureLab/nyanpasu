from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.models import PollEventCursor

if TYPE_CHECKING:
    from pathlib import Path


class GitHubReviewerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS github_poll_event_cursors (
                    repo TEXT PRIMARY KEY,
                    last_event_created_at TEXT NOT NULL,
                    cursor_event_ids_json TEXT NOT NULL DEFAULT '[]',
                    initialized_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def get_poll_event_cursor(self, repo: str) -> PollEventCursor | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo, last_event_created_at, cursor_event_ids_json, initialized_at, updated_at
                FROM github_poll_event_cursors WHERE repo = ?
                """,
                (repo,),
            ).fetchone()
        return _cursor_from_row(row) if row is not None else None

    def upsert_poll_event_cursor(
        self,
        repo: str,
        *,
        last_event_created_at: str,
        cursor_event_ids: tuple[str, ...],
    ) -> PollEventCursor:
        now = time.time()
        previous = self.get_poll_event_cursor(repo)
        initialized_at = previous.initialized_at if previous is not None else now
        cursor = PollEventCursor(
            repo=repo,
            last_event_created_at=last_event_created_at,
            cursor_event_ids=cursor_event_ids,
            initialized_at=initialized_at,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_poll_event_cursors (
                    repo, last_event_created_at, cursor_event_ids_json, initialized_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo) DO UPDATE SET
                    last_event_created_at = excluded.last_event_created_at,
                    cursor_event_ids_json = excluded.cursor_event_ids_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.repo,
                    cursor.last_event_created_at,
                    json.dumps(list(cursor.cursor_event_ids), ensure_ascii=False),
                    cursor.initialized_at,
                    cursor.updated_at,
                ),
            )
        return cursor


def _cursor_from_row(row: sqlite3.Row) -> PollEventCursor:
    try:
        raw_ids = json.loads(row["cursor_event_ids_json"])
    except json.JSONDecodeError:
        raw_ids = []
    event_ids = tuple(str(item) for item in raw_ids if isinstance(item, str))
    return PollEventCursor(
        repo=str(row["repo"]),
        last_event_created_at=str(row["last_event_created_at"]),
        cursor_event_ids=event_ids,
        initialized_at=float(row["initialized_at"]),
        updated_at=float(row["updated_at"]),
    )
