from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from nyanpasu.models import (
    AgentContext,
    AgentTask,
    CoalescedTaskRecord,
    ContextLease,
    ContextScope,
    DashboardPluginSummary,
    DashboardSnapshot,
    DashboardTaskItem,
    DashboardTotals,
    SubtaskRequest,
    TaskAction,
    TaskRunResult,
    TaskRunSummary,
    TaskStatus,
    json_dumps,
)

# A coalesced task is executed by its parent; do not maintain a second backend binding.
TASK_RUNS = """
    SELECT r.task_id, r.dedupe_key, r.context_key, r.action, r.status, r.event_worktree,
           r.thread_id, r.turn_id, r.task_json, r.coalesced_into, r.error, r.created_at, r.updated_at,
           r.spawned_by_task_id, r.context_generation,
           coalesce(parent.backend, r.backend) AS backend
    FROM task_runs r LEFT JOIN task_runs parent ON parent.task_id=r.coalesced_into
"""


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
                    coalesced_into TEXT,
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
            conn.execute("BEGIN IMMEDIATE")
            self._remove_conversation_copies(conn)
            for table in ("agent_contexts", "task_runs"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "backend" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN backend TEXT NOT NULL DEFAULT 'codex'")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
            for name, definition in {
                "spawned_by_task_id": "TEXT",
                "context_generation": "INTEGER NOT NULL DEFAULT 1",
                "wait_for": "TEXT",
                "subtask_result": "TEXT",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {definition}")
            conn.execute("""CREATE TABLE IF NOT EXISTS context_scopes (
                context_key TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'active',
                parent_context_key TEXT, parent_generation INTEGER
            )""")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(subtask_requests)")}
            if "parent_context_key" in columns:
                conn.execute("ALTER TABLE subtask_requests RENAME TO legacy_subtask_requests")
            conn.execute("""CREATE TABLE IF NOT EXISTS subtask_requests (
                parent_task_id TEXT NOT NULL,
                request_key TEXT NOT NULL, request_json TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (parent_task_id, request_key)
            )""")
            if "parent_context_key" in columns:
                conn.execute("""INSERT INTO subtask_requests
                    SELECT r.spawned_by_task_id,s.request_key,s.request_json,s.task_id
                    FROM legacy_subtask_requests s JOIN task_runs r ON r.task_id=s.task_id""")
                conn.execute("DROP TABLE legacy_subtask_requests")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_spawned_by ON task_runs(spawned_by_task_id)")
            conn.execute("""INSERT OR IGNORE INTO context_scopes (context_key, generation)
                SELECT context_key, 1 FROM agent_contexts""")
            conn.execute("""INSERT OR IGNORE INTO context_scopes (context_key, generation)
                SELECT context_key, 1 FROM task_runs WHERE status IN ('queued', 'running', 'waiting')""")

    @staticmethod
    def _remove_conversation_copies(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        if "result_json" not in columns:
            return
        if "coalesced_into" not in columns:
            conn.execute("ALTER TABLE task_runs ADD COLUMN coalesced_into TEXT")
        conn.execute("UPDATE task_runs SET coalesced_into=json_extract(result_json,'$.coalesced_into')")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "transcript_tasks" in tables:
            conn.execute("""
                UPDATE task_runs SET
                    thread_id=coalesce(thread_id, (
                        SELECT s.thread_id FROM transcript_tasks t
                        JOIN transcript_sessions s USING(session_id) WHERE t.task_id=task_runs.task_id
                    )),
                    turn_id=coalesce(turn_id, (
                        SELECT t.turn_id FROM transcript_tasks t WHERE t.task_id=task_runs.task_id
                    ))
            """)
        conn.execute("ALTER TABLE task_runs DROP COLUMN result_json")
        for table in (
            "transcript_events",
            "transcript_entries",
            "transcript_changes",
            "transcript_chunks",
            "transcript_contents",
            "transcript_tasks",
            "transcript_sessions",
            "transcript_imports",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    def record_task(self, task: AgentTask, *, default_backend: str = "codex") -> bool:
        accepted, _ = self.enqueue_task(task, default_backend=default_backend)
        return accepted

    def enqueue_task(
        self, task: AgentTask, *, default_backend: str = "codex", coalesce_since: float | None = None
    ) -> tuple[bool, str | None]:
        """Record and optionally merge a task before another worker can claim either task."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id=? OR dedupe_key=?", (task.task_id, task.dedupe_key)
            ).fetchone():
                return False, None
            scope = self._ensure_scope(conn, task.context_key, reopen=task.action is TaskAction.RUN)
            if scope.lifecycle == "closing" and task.action is TaskAction.RUN:
                raise ValueError("context is closing")
            if task.spawned_by_task_id is not None:
                raise ValueError("child tasks must be created through create_subtask")
            task = task.model_copy(update={"context_generation": scope.generation})
            backend = default_backend
            if task.action is TaskAction.CLEANUP:
                context = conn.execute(
                    "SELECT backend FROM agent_contexts WHERE context_key=?", (task.context_key,)
                ).fetchone()
                if context is not None:
                    backend = context["backend"]
            try:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, dedupe_key, context_key, action, status, task_json, created_at, updated_at, backend,
                        context_generation
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        backend,
                        scope.generation,
                    ),
                )
            except sqlite3.IntegrityError:
                return False, None
            if coalesce_since is None or not task.coalesce_key or task.action is not TaskAction.RUN:
                return True, None
            row = conn.execute(
                """
                SELECT task_id, action, task_json, created_at, backend FROM task_runs
                WHERE context_key = ? AND status = ? AND task_id <> ? AND thread_id IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (task.context_key, TaskStatus.QUEUED.value, task.task_id),
            ).fetchone()
            if (
                row is None
                or row["action"] != TaskAction.RUN.value
                or row["created_at"] < coalesce_since
                or row["backend"] != backend
            ):
                return True, None
            queued = AgentTask.model_validate(json.loads(row["task_json"]))
            if queued.coalesce_key != task.coalesce_key or queued.metadata.get("plugin_id") != task.metadata.get(
                "plugin_id"
            ):
                return True, None
            conn.execute(
                "UPDATE task_runs SET status = ?, coalesced_into = ?, updated_at = ? WHERE task_id = ?",
                (
                    TaskStatus.COMPLETED.value,
                    queued.task_id,
                    now,
                    task.task_id,
                ),
            )
            return True, queued.task_id

    def mark_task_running(self, task_id: str, event_worktree: Path | None, backend: str | None = None) -> None:
        self._update_task(task_id, TaskStatus.RUNNING, event_worktree=event_worktree, backend=backend)

    def bind_task_event_worktree(self, task_id: str, path: Path) -> None:
        # Creation may finish during cancellation; record ownership without reviving the task.
        with self._connect() as conn:
            conn.execute(
                """UPDATE task_runs SET event_worktree=?,updated_at=? WHERE task_id=? AND EXISTS (
                    SELECT 1 FROM context_scopes c WHERE c.context_key=task_runs.context_key
                    AND c.generation=task_runs.context_generation AND c.lifecycle <> 'closed'
                )""",
                (str(path), time.time(), task_id),
            )

    @staticmethod
    def _ensure_scope(conn: sqlite3.Connection, key: str, *, reopen: bool = False) -> ContextScope:
        conn.execute("INSERT OR IGNORE INTO context_scopes (context_key,generation) VALUES (?,1)", (key,))
        row = conn.execute("SELECT * FROM context_scopes WHERE context_key=?", (key,)).fetchone()
        scope = ContextScope.model_validate(dict(row))
        if reopen and scope.lifecycle == "closed":
            conn.execute(
                "UPDATE context_scopes SET generation=generation+1,lifecycle='active' WHERE context_key=?", (key,)
            )
            scope = scope.model_copy(update={"generation": scope.generation + 1, "lifecycle": "active"})
        return scope

    def context_scope(self, key: str) -> ContextScope:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM context_scopes WHERE context_key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("unknown context")
        return ContextScope.model_validate(dict(row))

    @staticmethod
    def _active_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute(
            """SELECT r.* FROM task_runs r JOIN context_scopes c USING(context_key)
            WHERE r.task_id=? AND r.context_generation=c.generation AND c.lifecycle='active'
            AND r.status IN ('queued','running','waiting')""",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError("task is inactive or its context is closing")
        return row

    def task_is_active(self, task_id: str) -> bool:
        with self._connect() as conn:
            try:
                self._active_task(conn, task_id)
            except ValueError:
                return False
        return True

    def create_subtask(self, parent_id: str, request: SubtaskRequest) -> AgentTask:
        """Create ownership and execution together; retries cannot leave an orphan."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = self._active_task(conn, parent_id)
            identity = (parent_id, request.request_key)
            serialized = request.model_dump_json()
            previous = conn.execute(
                """SELECT task_id,request_json FROM subtask_requests
                WHERE parent_task_id=? AND request_key=?""",
                identity,
            ).fetchone()
            if previous is not None:
                if previous["request_json"] != serialized:
                    raise ValueError("subtask request key was used with different input")
                row = conn.execute("SELECT task_json FROM task_runs WHERE task_id=?", (previous["task_id"],)).fetchone()
                return AgentTask.model_validate(json.loads(row["task_json"]))
            if parent["status"] != "running":
                raise ValueError("only a running task may create a subtask")
            original = AgentTask.model_validate(json.loads(parent["task_json"]))
            workspace = original.workspace
            if request.revision is not None:
                if workspace is None:
                    raise ValueError("a revision requires a repository workspace")
                workspace = workspace.model_copy(update={"revision": request.revision, "ref": None})
            task_id = str(uuid4())
            child = AgentTask(
                task_id=task_id,
                action=TaskAction.RUN,
                context_key=f"{original.context_key}/subtask/{task_id}",
                prompt=request.prompt,
                developer_instructions=request.developer_instructions,
                workspace=workspace,
                spawned_by_task_id=parent_id,
                metadata={
                    "purpose": request.purpose,
                    "request": {"title": request.purpose},
                    "source_plugin_id": original.metadata.get("plugin_id", original.metadata.get("source_plugin_id")),
                },
            )
            conn.execute(
                """INSERT INTO context_scopes
                (context_key,generation,parent_context_key,parent_generation) VALUES (?,1,?,?)""",
                (child.context_key, parent["context_key"], parent["context_generation"]),
            )
            now = time.time()
            conn.execute(
                """INSERT INTO task_runs
                (task_id,context_key,action,status,task_json,created_at,updated_at,backend,spawned_by_task_id)
                VALUES (?,?,?,'queued',?,?,?,?,?)""",
                (
                    task_id,
                    child.context_key,
                    child.action.value,
                    child.model_dump_json(),
                    now,
                    now,
                    parent["backend"],
                    parent_id,
                ),
            )
            conn.execute("INSERT INTO subtask_requests VALUES (?,?,?,?)", (*identity, serialized, task_id))
            return child

    def root_task_id(self, task_id: str) -> str:
        with self._connect() as conn:
            while True:
                row = conn.execute("SELECT spawned_by_task_id FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    raise ValueError("unknown task")
                if row["spawned_by_task_id"] is None:
                    return task_id
                task_id = row["spawned_by_task_id"]

    @staticmethod
    def _descendants(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            """WITH RECURSIVE children AS (
            SELECT * FROM task_runs WHERE spawned_by_task_id=?
            UNION ALL SELECT r.* FROM task_runs r JOIN children c ON r.spawned_by_task_id=c.task_id
        ) SELECT * FROM children ORDER BY created_at,task_id""",
            (task_id,),
        ).fetchall()

    def subtasks(self, task_id: str) -> list[TaskRunSummary]:
        with self._connect() as conn:
            return [_task_summary_from_row(row) for row in self._descendants(conn, task_id)]

    def unfinished_roots(self) -> list[AgentTask]:
        return [
            task
            for task in self.unfinished_tasks()
            if task.spawned_by_task_id is None and self.task_is_active(task.task_id)
        ]

    def wait_for_subtasks(self, task_id: str, children: list[str]) -> None:
        if not children or len(children) != len(set(children)):
            raise ValueError("specify distinct descendant task IDs")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._active_task(conn, task_id)
            descendants = {row["task_id"] for row in self._descendants(conn, task_id)}
            if not set(children) <= descendants:
                raise ValueError("may only wait for descendant tasks")
            conn.execute("UPDATE task_runs SET wait_for=? WHERE task_id=?", (json_dumps(children), task_id))

    def waiting_for(self, task_id: str) -> list[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT wait_for FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row["wait_for"]) if row and row["wait_for"] else []

    def wait_is_ready(self, task_id: str) -> bool:
        children = self.waiting_for(task_id)
        return all(self.task_status(child) in {"completed", "failed", "cancelled"} for child in children)

    def mark_task_waiting(self, task_id: str) -> None:
        self._update_task(task_id, TaskStatus.WAITING)

    def resume_subtasks(self, task_id: str) -> list[TaskRunSummary]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = self._active_task(conn, task_id)
            children = json.loads(parent["wait_for"]) if parent["wait_for"] else []
            if not children:
                return []
            rows = [conn.execute("SELECT * FROM task_runs WHERE task_id=?", (child,)).fetchone() for child in children]
            if any(row["status"] not in {"completed", "failed", "cancelled"} for row in rows):
                raise ValueError("subtasks are not finished")
            request = AgentTask.model_validate(json.loads(parent["task_json"]))
            request = request.model_copy(update={"metadata": {**request.metadata, "resumed_subtasks": children}})
            conn.execute(
                """UPDATE task_runs SET wait_for=NULL,status='queued',task_json=?,updated_at=?
                WHERE task_id=?""",
                (request.model_dump_json(), time.time(), task_id),
            )
        return [_task_summary_from_row(row) for row in rows]

    def record_subtask_result(self, task_id: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._active_task(conn, task_id)
            serialized = json_dumps(result)
            if row["subtask_result"] is not None and row["subtask_result"] != serialized:
                raise ValueError("result is already frozen")
            conn.execute("UPDATE task_runs SET subtask_result=? WHERE task_id=?", (serialized, task_id))

    def subtask_result(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT subtask_result FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row["subtask_result"]) if row and row["subtask_result"] else None

    def begin_context_cleanup(self, key: str, generation: int | None = None) -> list[ContextScope]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scope = self._ensure_scope(conn, key)
            if scope.lifecycle == "closed" or (generation is not None and scope.generation != generation):
                return []
            rows = conn.execute(
                """WITH RECURSIVE tree AS (
                SELECT * FROM context_scopes WHERE context_key=? AND generation=?
                UNION ALL SELECT c.* FROM context_scopes c JOIN tree p
                    ON c.parent_context_key=p.context_key AND c.parent_generation=p.generation
                    WHERE c.lifecycle <> 'closed'
            ) SELECT * FROM tree""",
                (key, scope.generation),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE context_scopes SET lifecycle='closing' WHERE context_key=?", (row["context_key"],))
            return [ContextScope.model_validate({**dict(row), "lifecycle": "closing"}) for row in rows]

    def closing_contexts(self) -> list[ContextScope]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM context_scopes WHERE lifecycle='closing' ORDER BY context_key"
            ).fetchall()
        return [ContextScope.model_validate(dict(row)) for row in rows]

    def tasks_for_scope(self, key: str, generation: int) -> list[TaskRunSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ({TASK_RUNS}) WHERE context_key=? AND context_generation=? ORDER BY created_at DESC",
                (key, generation),
            ).fetchall()
        return [_task_summary_from_row(row) for row in rows]

    def close_context_scope(self, key: str, generation: int) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scope = conn.execute(
                "SELECT generation,lifecycle FROM context_scopes WHERE context_key=?", (key,)
            ).fetchone()
            if scope is None or scope["generation"] != generation or scope["lifecycle"] != "closing":
                return
            descendants = conn.execute(
                """SELECT 1 FROM context_scopes WHERE parent_context_key=?
                AND parent_generation=? AND lifecycle <> 'closed'""",
                (key, generation),
            ).fetchone()
            if descendants:
                raise ValueError("context still has live descendants")
            conn.execute(
                """UPDATE task_runs SET status='cancelled',updated_at=?
                WHERE context_key=? AND context_generation=? AND status IN ('queued','running','waiting')
                AND action <> 'cleanup'""",
                (time.time(), key, generation),
            )
            conn.execute("DELETE FROM agent_contexts WHERE context_key=?", (key,))
            conn.execute(
                """UPDATE context_scopes SET lifecycle='closed'
                WHERE context_key=? AND generation=? AND lifecycle='closing'""",
                (key, generation),
            )

    def cancel_task_tree(self, task_id: str) -> list[str]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = [task_id, *(row["task_id"] for row in self._descendants(conn, task_id))]
            self._cancel_tasks(conn, ids)
        return ids

    @staticmethod
    def _cancel_tasks(conn: sqlite3.Connection, ids: list[str]) -> None:
        conn.executemany(
            """UPDATE task_runs SET status='cancelled',updated_at=?
            WHERE task_id=? AND status IN ('queued','running','waiting')""",
            [(time.time(), identity) for identity in ids],
        )

    def update_pending_task_backend(self, task_id: str, backend: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE task_runs SET backend=? WHERE task_id=?
                   AND status IN ('queued', 'running') AND thread_id IS NULL""",
                (backend, task_id),
            )

    def update_task_input(self, task: AgentTask) -> None:
        with self._connect() as conn:
            # Persist the execution request so an interrupted turn can resume with the same instructions.
            conn.execute(
                "UPDATE task_runs SET task_json = ?, action = ?, updated_at = ? WHERE task_id = ?",
                (json_dumps(_task_to_json(task)), task.action.value, time.time(), task.task_id),
            )

    def bind_task_execution(
        self,
        task_id: str,
        thread_id: str,
        turn_id: str | None,
        backend: str = "codex",
        *,
        context: AgentContext | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """SELECT 1 FROM task_runs t JOIN context_scopes c
                ON t.context_key=c.context_key AND t.context_generation=c.generation
                WHERE t.task_id=? AND c.lifecycle <> 'closed'""",
                (task_id,),
            ).fetchone()
            if current is None:
                return
            conn.execute(
                "UPDATE task_runs SET backend=?,thread_id=?,turn_id=coalesce(?,turn_id),updated_at=? WHERE task_id=?",
                (backend, thread_id, turn_id, time.time(), task_id),
            )
            if context is not None:
                self._upsert_context(conn, replace_context(context, thread_id=thread_id))

    def mark_task_done(self, result: TaskRunResult) -> bool:
        return self._update_task(
            result.task_id,
            result.status,
            backend=result.backend,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            event_worktree=result.event_worktree,
            error=result.error,
            final_message=result.final_message,
        )

    def mark_task_failed(self, task_id: str, error: str) -> None:
        self._update_task(task_id, TaskStatus.FAILED, error=error)

    def mark_task_interrupted(self, task_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE task_runs SET status='queued', error=?, updated_at=?
                   WHERE task_id=? AND status IN ('queued', 'running')""",
                (error, time.time(), task_id),
            )

    def unfinished_tasks(self) -> list[AgentTask]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT task_json FROM task_runs
                   WHERE status IN ('queued', 'running', 'waiting') AND coalesced_into IS NULL
                   ORDER BY created_at, task_id"""
            ).fetchall()
        return [AgentTask.model_validate(json.loads(row["task_json"])) for row in rows]

    def task_run(self, task_id: str) -> TaskRunSummary:
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM ({TASK_RUNS}) WHERE task_id=?", (task_id,)).fetchone()
        return _task_summary_from_row(row)

    def task_request(self, task_id: str) -> AgentTask:
        with self._connect() as conn:
            row = conn.execute("SELECT task_json FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        return AgentTask.model_validate(json.loads(row["task_json"]))

    def task_status(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
        return str(row["status"]) if row is not None else None

    def task_backend(self, task_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(f"SELECT backend FROM ({TASK_RUNS}) WHERE task_id=?", (task_id,)).fetchone()
        return row["backend"]

    def find_task_by_dedupe_key(self, dedupe_key: str) -> TaskRunSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT task_id, dedupe_key, context_key, backend, action, status, event_worktree, thread_id, turn_id, error,
                    created_at, updated_at
                FROM ({TASK_RUNS}) WHERE dedupe_key = ?
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
            SELECT task_id, dedupe_key, context_key, backend, action, status, event_worktree, thread_id, turn_id, error,
                created_at, updated_at
            FROM ({TASK_RUNS})
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

    def release_context_leases_for_owner(self, owner_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM context_leases WHERE owner_id = ?", (owner_id,))
            return cur.rowcount

    def coalesced_tasks_for(self, active_task_id: str) -> list[CoalescedTaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, task_json, created_at
                FROM task_runs
                WHERE coalesced_into = ?
                ORDER BY created_at ASC
                """,
                (active_task_id,),
            ).fetchall()
        tasks: list[CoalescedTaskRecord] = []
        for row in rows:
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
                SELECT context_key, backend, thread_id, session_worktree, workspace_key, revision
                FROM agent_contexts WHERE context_key = ?
                """,
                (context_key,),
            ).fetchone()
        if row is None:
            return None
        return AgentContext(
            backend=row["backend"],
            context_key=row["context_key"],
            thread_id=row["thread_id"],
            session_worktree=Path(row["session_worktree"]) if row["session_worktree"] else None,
            workspace_key=row["workspace_key"],
            revision=row["revision"],
        )

    def upsert_context(self, context: AgentContext) -> None:
        with self._connect() as conn:
            self._upsert_context(conn, context)

    @staticmethod
    def _upsert_context(conn: sqlite3.Connection, context: AgentContext) -> None:
        now = time.time()
        conn.execute(
            """
            INSERT INTO agent_contexts (
                context_key, backend, thread_id, session_worktree, workspace_key, revision, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_key) DO UPDATE SET
                backend = excluded.backend,
                thread_id = excluded.thread_id,
                session_worktree = excluded.session_worktree,
                workspace_key = excluded.workspace_key,
                revision = excluded.revision,
                updated_at = excluded.updated_at
            """,
            (
                context.context_key,
                context.backend,
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
                SELECT context_key, backend, thread_id, session_worktree, workspace_key, revision
                FROM agent_contexts ORDER BY context_key
                """
            ).fetchall()
        return [
            AgentContext(
                backend=row["backend"],
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
                f"""
                SELECT task_id, dedupe_key, context_key, backend, action, status, event_worktree, thread_id, turn_id, error,
                    created_at, updated_at
                FROM ({TASK_RUNS}) ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_task_summary_from_row(row) for row in rows]

    def dashboard_snapshot(self, *, recent_limit: int = 50, backlog_limit: int = 100) -> DashboardSnapshot:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT task_id, dedupe_key, context_key, backend, action, status, event_worktree, thread_id, turn_id, error,
                    created_at, updated_at, task_json
                FROM ({TASK_RUNS})
                ORDER BY updated_at DESC
                """
            ).fetchall()
            context_count = int(conn.execute("SELECT COUNT(*) AS count FROM agent_contexts").fetchone()["count"])
            active_lease_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM context_leases WHERE expires_at > ?",
                    (now,),
                ).fetchone()["count"]
            )

        items = [_dashboard_task_from_row(row, now=now) for row in rows]
        status_counts = {status.value: 0 for status in TaskStatus}
        action_counts = {action.value: 0 for action in TaskAction}
        plugin_counts: dict[str, DashboardPluginSummary] = {}
        for item in items:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
            action_counts[item.action] = action_counts.get(item.action, 0) + 1
            current = plugin_counts.get(item.plugin_id)
            if current is None:
                current = DashboardPluginSummary(plugin_id=item.plugin_id)
            plugin_counts[item.plugin_id] = current.model_copy(
                update={
                    "total": current.total + 1,
                    item.status: getattr(current, item.status, 0) + 1,
                    "last_updated_at": max(current.last_updated_at or 0, item.updated_at),
                }
            )

        queued = status_counts.get(TaskStatus.QUEUED.value, 0)
        running = status_counts.get(TaskStatus.RUNNING.value, 0)
        completed = status_counts.get(TaskStatus.COMPLETED.value, 0)
        failed = status_counts.get(TaskStatus.FAILED.value, 0)
        backlog = sorted(
            [item for item in items if item.status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}],
            key=lambda item: (item.status != TaskStatus.RUNNING.value, item.created_at),
        )[:backlog_limit]
        plugins = tuple(sorted(plugin_counts.values(), key=lambda item: item.last_updated_at or 0, reverse=True))
        return DashboardSnapshot(
            generated_at=now,
            totals=DashboardTotals(
                total=len(items),
                queued=queued,
                running=running,
                completed=completed,
                failed=failed,
                backlog=queued + running,
                contexts=context_count,
                active_leases=active_lease_count,
            ),
            status_counts=status_counts,
            action_counts=action_counts,
            plugins=plugins,
            backlog=tuple(backlog),
            recent=tuple(items[:recent_limit]),
        )

    def _update_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        event_worktree: Path | None = None,
        backend: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        error: str | None = None,
        final_message: str | None = None,
    ) -> bool:
        updates = ["status = ?", "updated_at = ?", "error = ?"]
        values: list[Any] = [status.value, time.time(), error]
        if status is TaskStatus.COMPLETED and final_message is not None:
            updates.append(
                "subtask_result = CASE WHEN spawned_by_task_id IS NOT NULL "
                "THEN coalesce(subtask_result, ?) ELSE subtask_result END"
            )
            values.append(json_dumps({"summary": final_message, "artifacts": [], "data": {}}))
        if backend is not None:
            updates.append("backend = ?")
            values.append(backend)
        if event_worktree is not None:
            updates.append("event_worktree = ?")
            values.append(str(event_worktree))
        if thread_id is not None:
            updates.append("thread_id = ?")
            values.append(thread_id)
        if turn_id is not None:
            updates.append("turn_id = ?")
            values.append(turn_id)
        values.append(task_id)
        with self._connect() as conn:
            updated = conn.execute(
                f"""UPDATE task_runs SET {", ".join(updates)} WHERE task_id = ? AND status <> 'cancelled'
                    AND (action IN ('cleanup','ignored') OR EXISTS (
                        SELECT 1 FROM context_scopes c WHERE c.context_key=task_runs.context_key
                        AND c.generation=task_runs.context_generation AND c.lifecycle='active'
                    ))""",
                values,
            )
            if status is TaskStatus.FAILED and updated.rowcount:
                # A restart must never see a failed owner with active descendants.
                self._cancel_tasks(conn, [row["task_id"] for row in self._descendants(conn, task_id)])
            return bool(updated.rowcount)


def replace_context(context: AgentContext, **changes: Any) -> AgentContext:
    return context.model_copy(update=changes)


def _task_summary_from_row(row: sqlite3.Row) -> TaskRunSummary:
    return TaskRunSummary.model_validate(dict(row))


def _dashboard_task_from_row(row: sqlite3.Row, *, now: float) -> DashboardTaskItem:
    task = _json_object(row["task_json"])
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    assert isinstance(metadata, dict)
    return DashboardTaskItem(
        task_id=str(row["task_id"]),
        dedupe_key=str(row["dedupe_key"]) if row["dedupe_key"] is not None else None,
        plugin_id=_dashboard_plugin_id(metadata),
        action=str(row["action"]),
        status=str(row["status"]),
        context_key=str(row["context_key"]),
        title=_dashboard_title(task, metadata),
        source=_dashboard_source(metadata),
        thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
        turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        age_seconds=max(0.0, now - float(row["created_at"])),
    )


def _json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dashboard_plugin_id(metadata: dict[str, Any]) -> str:
    plugin_id = metadata.get("plugin_id")
    if isinstance(plugin_id, str) and plugin_id.strip():
        return plugin_id.strip()
    return "core"


def _dashboard_title(task: dict[str, Any], metadata: dict[str, Any]) -> str:
    request = metadata.get("request")
    if isinstance(request, dict):
        title = request.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        task_text = request.get("task")
        if isinstance(task_text, str) and task_text.strip():
            return _first_line(task_text)
    pull_request = metadata.get("pull_request")
    if isinstance(pull_request, dict):
        repo = pull_request.get("repo")
        number = pull_request.get("number")
        event = metadata.get("github_event")
        mode = metadata.get("review_mode")
        parts = [str(repo) if repo else "GitHub PR", f"#{number}" if number else ""]
        suffix = " ".join(str(value) for value in (event, mode) if isinstance(value, str) and value)
        return f"{' '.join(part for part in parts if part).strip()} {suffix}".strip()
    prompt = task.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return _first_line(prompt)
    task_id = task.get("task_id")
    return str(task_id) if task_id else "Task"


def _dashboard_source(metadata: dict[str, Any]) -> str | None:
    request = metadata.get("request")
    if isinstance(request, dict):
        repo = request.get("repo")
        if isinstance(repo, str) and repo:
            return repo
    pull_request = metadata.get("pull_request")
    if isinstance(pull_request, dict):
        repo = pull_request.get("repo")
        number = pull_request.get("number")
        if repo and number:
            return f"{repo}#{number}"
        if repo:
            return str(repo)
    return None


def _first_line(value: str) -> str:
    line = " ".join(value.strip().splitlines()[0].split())
    if len(line) > 140:
        return line[:137] + "..."
    return line or "Task"


def _task_to_json(task: AgentTask) -> dict[str, Any]:
    return task.model_dump(mode="json")
