from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path


class PullRequestRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    repo: str
    branch_name: str
    status: str
    pr_url: str | None = None
    pr_number: int | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: float
    updated_at: float


class ManagedPullRequestRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    context_key: str
    repo: str
    pr_number: int
    pr_url: str
    base_branch: str
    branch_name: str
    title: str
    body: str
    task: str
    git_author_name: str | None = None
    git_author_email: str | None = None
    last_digest: str | None = None
    last_head_sha: str | None = None
    last_checked_at: float | None = None
    active: bool = True
    created_at: float
    updated_at: float


class GitHubPrMakerStore:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_pr_maker_records (
                    task_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pr_url TEXT,
                    pr_number INTEGER,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(github_pr_maker_records)").fetchall()
            }
            if "pr_number" not in columns:
                conn.execute("ALTER TABLE github_pr_maker_records ADD COLUMN pr_number INTEGER")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_pr_maker_managed_prs (
                    task_id TEXT PRIMARY KEY,
                    context_key TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    pr_url TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    task TEXT NOT NULL,
                    git_author_name TEXT,
                    git_author_email TEXT,
                    last_digest TEXT,
                    last_head_sha TEXT,
                    last_checked_at REAL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(github_pr_maker_managed_prs)").fetchall()
            }
            for column, ddl in {
                "git_author_name": "ALTER TABLE github_pr_maker_managed_prs ADD COLUMN git_author_name TEXT",
                "git_author_email": "ALTER TABLE github_pr_maker_managed_prs ADD COLUMN git_author_email TEXT",
            }.items():
                if column not in columns:
                    conn.execute(ddl)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_github_pr_maker_managed_prs_repo_pr
                ON github_pr_maker_managed_prs(repo, pr_number)
                """
            )

    def upsert_result(
        self,
        *,
        task_id: str,
        repo: str,
        branch_name: str,
        status: str,
        pr_url: str | None,
        pr_number: int | None = None,
        result: dict[str, Any],
        error: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_pr_maker_records (
                    task_id, repo, branch_name, status, pr_url, pr_number, result_json, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    repo = excluded.repo,
                    branch_name = excluded.branch_name,
                    status = excluded.status,
                    pr_url = excluded.pr_url,
                    pr_number = excluded.pr_number,
                    result_json = excluded.result_json,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    repo,
                    branch_name,
                    status,
                    pr_url,
                    pr_number,
                    json.dumps(result, ensure_ascii=False),
                    error,
                    now,
                    now,
                ),
            )

    def get(self, task_id: str) -> PullRequestRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT task_id, repo, branch_name, status, pr_url, pr_number, result_json, error, created_at, updated_at
                FROM github_pr_maker_records WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(str(row["result_json"]))
        except json.JSONDecodeError:
            result = {}
        return PullRequestRecord(
            task_id=str(row["task_id"]),
            repo=str(row["repo"]),
            branch_name=str(row["branch_name"]),
            status=str(row["status"]),
            pr_url=str(row["pr_url"]) if row["pr_url"] is not None else None,
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            result=result if isinstance(result, dict) else {},
            error=str(row["error"]) if row["error"] is not None else None,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def upsert_managed_pr(
        self,
        *,
        task_id: str,
        context_key: str,
        repo: str,
        pr_number: int,
        pr_url: str,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
        task: str,
        git_author_name: str | None,
        git_author_email: str | None,
        last_digest: str | None,
        last_head_sha: str | None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_pr_maker_managed_prs (
                    task_id, context_key, repo, pr_number, pr_url, base_branch, branch_name, title, body, task,
                    git_author_name, git_author_email, last_digest, last_head_sha, last_checked_at, active,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    context_key = excluded.context_key,
                    repo = excluded.repo,
                    pr_number = excluded.pr_number,
                    pr_url = excluded.pr_url,
                    base_branch = excluded.base_branch,
                    branch_name = excluded.branch_name,
                    title = excluded.title,
                    body = excluded.body,
                    task = excluded.task,
                    git_author_name = excluded.git_author_name,
                    git_author_email = excluded.git_author_email,
                    last_digest = excluded.last_digest,
                    last_head_sha = excluded.last_head_sha,
                    last_checked_at = excluded.last_checked_at,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    context_key,
                    repo,
                    pr_number,
                    pr_url,
                    base_branch,
                    branch_name,
                    title,
                    body,
                    task,
                    git_author_name,
                    git_author_email,
                    last_digest,
                    last_head_sha,
                    now,
                    now,
                    now,
                ),
            )

    def list_active_managed_prs(self) -> tuple[ManagedPullRequestRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, context_key, repo, pr_number, pr_url, base_branch, branch_name, title, body, task,
                    git_author_name, git_author_email, last_digest, last_head_sha, last_checked_at, active,
                    created_at, updated_at
                FROM github_pr_maker_managed_prs
                WHERE active = 1
                ORDER BY updated_at ASC
                """
            ).fetchall()
        return tuple(_managed_pr_from_row(row) for row in rows)

    def update_managed_pr_cursor(
        self,
        *,
        task_id: str,
        last_digest: str,
        last_head_sha: str,
        active: bool,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE github_pr_maker_managed_prs
                SET last_digest = ?, last_head_sha = ?, last_checked_at = ?, active = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (last_digest, last_head_sha, now, 1 if active else 0, now, task_id),
            )


def _managed_pr_from_row(row: sqlite3.Row) -> ManagedPullRequestRecord:
    return ManagedPullRequestRecord(
        task_id=str(row["task_id"]),
        context_key=str(row["context_key"]),
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        pr_url=str(row["pr_url"]),
        base_branch=str(row["base_branch"]),
        branch_name=str(row["branch_name"]),
        title=str(row["title"]),
        body=str(row["body"]),
        task=str(row["task"]),
        git_author_name=str(row["git_author_name"]) if row["git_author_name"] is not None else None,
        git_author_email=str(row["git_author_email"]) if row["git_author_email"] is not None else None,
        last_digest=str(row["last_digest"]) if row["last_digest"] is not None else None,
        last_head_sha=str(row["last_head_sha"]) if row["last_head_sha"] is not None else None,
        last_checked_at=float(row["last_checked_at"]) if row["last_checked_at"] is not None else None,
        active=bool(row["active"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
