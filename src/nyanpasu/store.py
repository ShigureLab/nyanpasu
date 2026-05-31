from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from nyanpasu.models import (
    AgentContext,
    AgentTask,
    CoalescedTaskRecord,
    ContextLease,
    TaskRunResult,
    TaskRunSummary,
    TaskStatus,
    json_dumps,
)


class StateStore:
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
                CREATE TABLE IF NOT EXISTS agent_contexts (
                    context_key TEXT PRIMARY KEY,
                    thread_id TEXT,
                    session_worktree TEXT,
                    workspace_key TEXT,
                    revision TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_runs (
                    task_id TEXT PRIMARY KEY,
                    dedupe_key TEXT,
                    context_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    event_worktree TEXT,
                    thread_id TEXT,
                    turn_id TEXT,
                    task_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_dedupe_key
                ON task_runs(dedupe_key)
                WHERE dedupe_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS context_leases (
                    context_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )

    def record_task(self, task: AgentTask) -> bool:
        now = time.time()
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, dedupe_key, context_key, action, status, task_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.dedupe_key,
                        task.context_key,
                        task.action.value,
                        TaskStatus.QUEUED.value,
                        json_dumps(_task_to_json(task)),
                        now,
                        now,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_task_running(self, task_id: str, event_worktree: Path | None) -> None:
        self._update_task(task_id, TaskStatus.RUNNING, event_worktree=event_worktree)

    def mark_task_done(self, result: TaskRunResult) -> None:
        self._update_task(
            result.task_id,
            result.status,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            result_json=json_dumps(_result_to_json(result)),
            error=result.error,
        )

    def mark_task_failed(self, task_id: str, error: str) -> None:
        self._update_task(task_id, TaskStatus.FAILED, error=error)

    def mark_task_coalesced(self, task_id: str, active_task_id: str) -> None:
        self._update_task(
            task_id,
            TaskStatus.COMPLETED,
            result_json=json_dumps({"coalesced": True, "coalesced_into": active_task_id}),
        )

    def task_status(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
        return str(row["status"]) if row is not None else None

    def find_task_by_dedupe_key(self, dedupe_key: str) -> TaskRunSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT task_id, dedupe_key, context_key, action, status, event_worktree, thread_id, turn_id, error,
                    created_at, updated_at
                FROM task_runs WHERE dedupe_key = ?
                """,
                (dedupe_key,),
            ).fetchone()
        return _task_summary_from_row(row) if row is not None else None

    def active_task_for_context(
        self,
        context_key: str,
        *,
        exclude_task_id: str | None = None,
        since: float | None = None,
        statuses: tuple[str, ...] = (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
    ) -> TaskRunSummary | None:
        if not statuses:
            return None
        where = ["context_key = ?", f"status IN ({', '.join('?' for _ in statuses)})"]
        values: list[Any] = [context_key, *statuses]
        if exclude_task_id is not None:
            where.append("task_id <> ?")
            values.append(exclude_task_id)
        if since is not None:
            where.append("created_at >= ?")
            values.append(since)
        sql = f"""
            SELECT task_id, dedupe_key, context_key, action, status, event_worktree, thread_id, turn_id, error,
                created_at, updated_at
            FROM task_runs
            WHERE {" AND ".join(where)}
            ORDER BY created_at ASC
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(sql, values).fetchone()
        return _task_summary_from_row(row) if row is not None else None

    def try_acquire_context_lease(
        self,
        context_key: str,
        *,
        owner_id: str,
        task_id: str,
        ttl_seconds: float,
    ) -> bool:
        now = time.time()
        expires_at = now + ttl_seconds
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO context_leases (
                        context_key, owner_id, task_id, acquired_at, heartbeat_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (context_key, owner_id, task_id, now, now, expires_at),
                )
                return True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT owner_id, task_id, expires_at FROM context_leases
                    WHERE context_key = ?
                    """,
                    (context_key,),
                ).fetchone()
                if row is None:
                    return False
                if str(row["owner_id"]) == owner_id and str(row["task_id"]) == task_id:
                    conn.execute(
                        """
                        UPDATE context_leases
                        SET heartbeat_at = ?, expires_at = ?
                        WHERE context_key = ? AND owner_id = ? AND task_id = ?
                        """,
                        (now, expires_at, context_key, owner_id, task_id),
                    )
                    return True
                if float(row["expires_at"]) > now:
                    return False
                cur = conn.execute(
                    """
                    UPDATE context_leases
                    SET owner_id = ?, task_id = ?, acquired_at = ?, heartbeat_at = ?, expires_at = ?
                    WHERE context_key = ? AND expires_at <= ?
                    """,
                    (owner_id, task_id, now, now, expires_at, context_key, now),
                )
                return cur.rowcount > 0

    def heartbeat_context_lease(
        self,
        context_key: str,
        *,
        owner_id: str,
        task_id: str,
        ttl_seconds: float,
    ) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE context_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE context_key = ? AND owner_id = ? AND task_id = ?
                """,
                (now, now + ttl_seconds, context_key, owner_id, task_id),
            )
            return cur.rowcount > 0

    def release_context_lease(self, context_key: str, *, owner_id: str, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM context_leases
                WHERE context_key = ? AND owner_id = ? AND task_id = ?
                """,
                (context_key, owner_id, task_id),
            )
            return cur.rowcount > 0

    def get_context_lease(self, context_key: str) -> ContextLease | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT context_key, owner_id, task_id, acquired_at, heartbeat_at, expires_at
                FROM context_leases WHERE context_key = ?
                """,
                (context_key,),
            ).fetchone()
        return ContextLease.model_validate(dict(row)) if row is not None else None

    def coalesced_tasks_for(self, active_task_id: str) -> list[CoalescedTaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, task_json, result_json, created_at
                FROM task_runs
                WHERE status = ? AND result_json IS NOT NULL
                ORDER BY created_at ASC
                """,
                (TaskStatus.COMPLETED.value,),
            ).fetchall()
        tasks: list[CoalescedTaskRecord] = []
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict) or result.get("coalesced_into") != active_task_id:
                continue
            try:
                task = json.loads(row["task_json"])
            except json.JSONDecodeError:
                task = {}
            if not isinstance(task, dict):
                task = {}
            tasks.append(
                CoalescedTaskRecord(
                    task_id=row["task_id"],
                    task=task,
                    created_at=float(row["created_at"]),
                )
            )
        return tasks

    def get_context(self, context_key: str) -> AgentContext | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT context_key, thread_id, session_worktree, workspace_key, revision
                FROM agent_contexts WHERE context_key = ?
                """,
                (context_key,),
            ).fetchone()
        if row is None:
            return None
        return AgentContext(
            context_key=row["context_key"],
            thread_id=row["thread_id"],
            session_worktree=Path(row["session_worktree"]) if row["session_worktree"] else None,
            workspace_key=row["workspace_key"],
            revision=row["revision"],
        )

    def upsert_context(self, context: AgentContext) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_contexts (
                    context_key, thread_id, session_worktree, workspace_key, revision, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(context_key) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    session_worktree = excluded.session_worktree,
                    workspace_key = excluded.workspace_key,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    context.context_key,
                    context.thread_id,
                    str(context.session_worktree) if context.session_worktree else None,
                    context.workspace_key,
                    context.revision,
                    now,
                    now,
                ),
            )

    def delete_context(self, context_key: str) -> AgentContext | None:
        context = self.get_context(context_key)
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_contexts WHERE context_key = ?", (context_key,))
        return context

    def list_contexts(self) -> list[AgentContext]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT context_key, thread_id, session_worktree, workspace_key, revision
                FROM agent_contexts ORDER BY context_key
                """
            ).fetchall()
        return [
            AgentContext(
                context_key=row["context_key"],
                thread_id=row["thread_id"],
                session_worktree=Path(row["session_worktree"]) if row["session_worktree"] else None,
                workspace_key=row["workspace_key"],
                revision=row["revision"],
            )
            for row in rows
        ]

    def recent_tasks(self, limit: int = 20) -> list[TaskRunSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, dedupe_key, context_key, action, status, event_worktree, thread_id, turn_id, error,
                    created_at, updated_at
                FROM task_runs ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_task_summary_from_row(row) for row in rows]

    def _update_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        event_worktree: Path | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        result_json: str | None = None,
        error: str | None = None,
    ) -> None:
        updates = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status.value, time.time()]
        if event_worktree is not None:
            updates.append("event_worktree = ?")
            values.append(str(event_worktree))
        if thread_id is not None:
            updates.append("thread_id = ?")
            values.append(thread_id)
        if turn_id is not None:
            updates.append("turn_id = ?")
            values.append(turn_id)
        if result_json is not None:
            updates.append("result_json = ?")
            values.append(result_json)
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        values.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE task_runs SET {', '.join(updates)} WHERE task_id = ?", values)


def replace_context(context: AgentContext, **changes: Any) -> AgentContext:
    return context.model_copy(update=changes)


def _task_summary_from_row(row: sqlite3.Row) -> TaskRunSummary:
    return TaskRunSummary.model_validate(dict(row))


def _task_to_json(task: AgentTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "action": task.action.value,
        "context_key": task.context_key,
        "prompt": task.prompt,
        "workspace": task.workspace.model_dump(mode="json") if task.workspace else None,
        "instruction_docs": [doc.model_dump(mode="json") for doc in task.instruction_docs],
        "dedupe_key": task.dedupe_key,
        "metadata": task.metadata,
        "workspace_policy": task.workspace_policy,
        "cleanup_policy": task.cleanup_policy,
    }


def _result_to_json(result: TaskRunResult) -> dict[str, Any]:
    return result.model_dump(mode="json")
