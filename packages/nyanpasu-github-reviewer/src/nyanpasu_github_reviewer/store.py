from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.models import (
    GitHubEventJournalRecord,
    GitHubEventJournalStatus,
    PollEventCursor,
    PullRequestSnapshot,
    PullRequestTimelineCursor,
    PullRequestUpdatedCursor,
    ReviewEvent,
)

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

                CREATE TABLE IF NOT EXISTS github_pr_updated_cursors (
                    repo TEXT PRIMARY KEY,
                    last_updated_at TEXT NOT NULL,
                    pr_node_ids_json TEXT NOT NULL DEFAULT '[]',
                    initialized_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS github_pr_snapshots (
                    repo TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    node_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    state TEXT NOT NULL,
                    draft INTEGER NOT NULL,
                    base_ref TEXT NOT NULL,
                    head_ref TEXT NOT NULL,
                    head_repo TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    title_hash TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    created_at_github TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    modified_at REAL NOT NULL,
                    PRIMARY KEY (repo, number)
                );

                CREATE TABLE IF NOT EXISTS github_pr_timeline_cursors (
                    repo TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    last_item_updated_at TEXT NOT NULL,
                    item_ids_json TEXT NOT NULL DEFAULT '[]',
                    initialized_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (repo, pr_number)
                );

                CREATE TABLE IF NOT EXISTS github_event_journal (
                    delivery_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    pr_number INTEGER,
                    github_event TEXT NOT NULL,
                    action TEXT NOT NULL,
                    event_created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            _ensure_column(conn, "github_pr_snapshots", "created_at_github", "TEXT NOT NULL DEFAULT ''")

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

    def get_pr_updated_cursor(self, repo: str) -> PullRequestUpdatedCursor | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo, last_updated_at, pr_node_ids_json, initialized_at, updated_at
                FROM github_pr_updated_cursors WHERE repo = ?
                """,
                (repo,),
            ).fetchone()
        return _pr_updated_cursor_from_row(row) if row is not None else None

    def upsert_pr_updated_cursor(
        self,
        repo: str,
        *,
        last_updated_at: str,
        pr_node_ids: tuple[str, ...],
    ) -> PullRequestUpdatedCursor:
        now = time.time()
        previous = self.get_pr_updated_cursor(repo)
        initialized_at = previous.initialized_at if previous is not None else now
        cursor = PullRequestUpdatedCursor(
            repo=repo,
            last_updated_at=last_updated_at,
            pr_node_ids=pr_node_ids,
            initialized_at=initialized_at,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_pr_updated_cursors (
                    repo, last_updated_at, pr_node_ids_json, initialized_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo) DO UPDATE SET
                    last_updated_at = excluded.last_updated_at,
                    pr_node_ids_json = excluded.pr_node_ids_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.repo,
                    cursor.last_updated_at,
                    json.dumps(list(cursor.pr_node_ids), ensure_ascii=False),
                    cursor.initialized_at,
                    cursor.updated_at,
                ),
            )
        return cursor

    def get_pr_snapshot(self, repo: str, number: int) -> PullRequestSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo, number, node_id, url, state, draft, base_ref, head_ref, head_repo, head_sha,
                    title_hash, body_hash, created_at_github, updated_at
                FROM github_pr_snapshots WHERE repo = ? AND number = ?
                """,
                (repo, number),
            ).fetchone()
        return _pr_snapshot_from_row(row) if row is not None else None

    def upsert_pr_snapshot(self, snapshot: PullRequestSnapshot) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_pr_snapshots (
                    repo, number, node_id, url, state, draft, base_ref, head_ref, head_repo, head_sha,
                    title_hash, body_hash, created_at_github, updated_at, created_at, modified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, number) DO UPDATE SET
                    node_id = excluded.node_id,
                    url = excluded.url,
                    state = excluded.state,
                    draft = excluded.draft,
                    base_ref = excluded.base_ref,
                    head_ref = excluded.head_ref,
                    head_repo = excluded.head_repo,
                    head_sha = excluded.head_sha,
                    title_hash = excluded.title_hash,
                    body_hash = excluded.body_hash,
                    created_at_github = excluded.created_at_github,
                    updated_at = excluded.updated_at,
                    modified_at = excluded.modified_at
                """,
                (
                    snapshot.repo,
                    snapshot.number,
                    snapshot.node_id,
                    snapshot.url,
                    snapshot.state,
                    int(snapshot.draft),
                    snapshot.base_ref,
                    snapshot.head_ref,
                    snapshot.head_repo,
                    snapshot.head_sha,
                    snapshot.title_hash,
                    snapshot.body_hash,
                    snapshot.created_at,
                    snapshot.updated_at,
                    now,
                    now,
                ),
            )

    def get_pr_timeline_cursor(self, repo: str, pr_number: int) -> PullRequestTimelineCursor | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo, pr_number, last_item_updated_at, item_ids_json, initialized_at, updated_at
                FROM github_pr_timeline_cursors WHERE repo = ? AND pr_number = ?
                """,
                (repo, pr_number),
            ).fetchone()
        return _pr_timeline_cursor_from_row(row) if row is not None else None

    def upsert_pr_timeline_cursor(
        self,
        repo: str,
        pr_number: int,
        *,
        last_item_updated_at: str,
        item_ids: tuple[str, ...],
    ) -> PullRequestTimelineCursor:
        now = time.time()
        previous = self.get_pr_timeline_cursor(repo, pr_number)
        initialized_at = previous.initialized_at if previous is not None else now
        cursor = PullRequestTimelineCursor(
            repo=repo,
            pr_number=pr_number,
            last_item_updated_at=last_item_updated_at,
            item_ids=item_ids,
            initialized_at=initialized_at,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_pr_timeline_cursors (
                    repo, pr_number, last_item_updated_at, item_ids_json, initialized_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, pr_number) DO UPDATE SET
                    last_item_updated_at = excluded.last_item_updated_at,
                    item_ids_json = excluded.item_ids_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.repo,
                    cursor.pr_number,
                    cursor.last_item_updated_at,
                    json.dumps(list(cursor.item_ids), ensure_ascii=False),
                    cursor.initialized_at,
                    cursor.updated_at,
                ),
            )
        return cursor

    def append_event(self, record: GitHubEventJournalRecord) -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO github_event_journal (
                        delivery_id, dedupe_key, source, repo, pr_number, github_event, action, event_created_at,
                        payload_json, status, result_json, error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.delivery_id,
                        record.dedupe_key,
                        record.source,
                        record.repo,
                        record.pr_number,
                        record.github_event,
                        record.action.value,
                        record.event_created_at,
                        record.event.model_dump_json(),
                        record.status.value,
                        record.result_json,
                        record.error,
                        record.created_at,
                        record.updated_at,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def pending_events(self, *, repo: str | None = None, limit: int | None = None) -> list[GitHubEventJournalRecord]:
        sql = """
            SELECT delivery_id, dedupe_key, source, repo, pr_number, github_event, action, event_created_at,
                payload_json, status, result_json, error, created_at, updated_at
            FROM github_event_journal
            WHERE status = ?
        """
        values: list[object] = [GitHubEventJournalStatus.PENDING.value]
        if repo is not None:
            sql += " AND repo = ?"
            values.append(repo)
        sql += " ORDER BY event_created_at ASC, created_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, values).fetchall()
        return [_journal_record_from_row(row) for row in rows]

    def mark_event_status(
        self,
        delivery_id: str,
        status: GitHubEventJournalStatus,
        *,
        result_json: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE github_event_journal
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (status.value, result_json, error, time.time(), delivery_id),
            )


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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _pr_updated_cursor_from_row(row: sqlite3.Row) -> PullRequestUpdatedCursor:
    try:
        raw_ids = json.loads(row["pr_node_ids_json"])
    except json.JSONDecodeError:
        raw_ids = []
    node_ids = tuple(str(item) for item in raw_ids if isinstance(item, str))
    return PullRequestUpdatedCursor(
        repo=str(row["repo"]),
        last_updated_at=str(row["last_updated_at"]),
        pr_node_ids=node_ids,
        initialized_at=float(row["initialized_at"]),
        updated_at=float(row["updated_at"]),
    )


def _pr_snapshot_from_row(row: sqlite3.Row) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repo=str(row["repo"]),
        number=int(row["number"]),
        node_id=str(row["node_id"]),
        url=str(row["url"]),
        state=str(row["state"]),
        draft=bool(row["draft"]),
        base_ref=str(row["base_ref"]),
        head_ref=str(row["head_ref"]),
        head_repo=str(row["head_repo"]),
        head_sha=str(row["head_sha"]),
        title_hash=str(row["title_hash"]),
        body_hash=str(row["body_hash"]),
        created_at=str(row["created_at_github"]),
        updated_at=str(row["updated_at"]),
    )


def _pr_timeline_cursor_from_row(row: sqlite3.Row) -> PullRequestTimelineCursor:
    try:
        raw_ids = json.loads(row["item_ids_json"])
    except json.JSONDecodeError:
        raw_ids = []
    item_ids = tuple(str(item) for item in raw_ids if isinstance(item, str))
    return PullRequestTimelineCursor(
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        last_item_updated_at=str(row["last_item_updated_at"]),
        item_ids=item_ids,
        initialized_at=float(row["initialized_at"]),
        updated_at=float(row["updated_at"]),
    )


def _journal_record_from_row(row: sqlite3.Row) -> GitHubEventJournalRecord:
    event = ReviewEvent.model_validate_json(str(row["payload_json"]))
    return GitHubEventJournalRecord(
        delivery_id=str(row["delivery_id"]),
        dedupe_key=str(row["dedupe_key"]),
        source=str(row["source"]),
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
        github_event=str(row["github_event"]),
        action=event.action,
        event_created_at=str(row["event_created_at"]),
        event=event,
        status=GitHubEventJournalStatus(str(row["status"])),
        result_json=str(row["result_json"]) if row["result_json"] is not None else None,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
