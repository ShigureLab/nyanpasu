from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from nyanpasu.models import SubtaskRequest
from nyanpasu.schema import SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path


def _kind(task: dict) -> str:
    metadata = task.get("metadata", {})
    plugin = metadata.get("plugin_id", metadata.get("source_plugin_id"))
    if task.get("spawned_by_task_id"):
        return f"{plugin}.{metadata.get('purpose', 'subtask')}" if plugin else "subtask"
    if plugin == "github_reviewer":
        return "github_reviewer.review"
    if plugin == "github_pr_maker":
        return "github_pr_maker.followup" if metadata.get("follow_up") else "github_pr_maker.create"
    return "default"


def migrate_state(
    path: Path, *, native_homes: dict[str, Path], isolated_homes: dict[str, Path] | None = None
) -> dict[str, int]:
    """One-time migration from the pre-hybrid schema, with no runtime fallback.

    The caller stops admissions and backs up SQLite before this operation. Only
    session/turn references are migrated; conversation text stays native. Supply
    the actual native history directories, which can differ from wrapper config
    directories. Reader boundaries default to those exact directories, never
    their parent HOME; isolated_homes permits explicit per-backend boundaries.
    """
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return {"tasks": 0, "contexts": 0, "turns": 0}
        if version != 0:
            raise ValueError(f"unsupported state schema version: {version}")
        active = conn.execute(
            "SELECT count(*) FROM task_runs WHERE status IN ('queued','running','waiting')"
        ).fetchone()[0]
        leases = conn.execute("SELECT count(*) FROM context_leases").fetchone()[0]
        if active or leases:
            raise ValueError("drain tasks and release context leases before migrating state")
        required = {"backend", "spawned_by_task_id", "context_generation", "wait_for", "subtask_result"}
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        request_columns = {row["name"] for row in conn.execute("PRAGMA table_info(subtask_requests)")}
        if not required <= columns or "parent_task_id" not in request_columns or "result_json" in columns:
            raise ValueError("migration requires the current pre-hybrid schema; restore or upgrade the input first")
        conn.execute("ALTER TABLE agent_contexts ADD COLUMN memory_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """CREATE TABLE task_turns (
                task_id TEXT NOT NULL, backend TEXT NOT NULL, thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, started_at REAL NOT NULL,
                PRIMARY KEY (task_id,backend,thread_id,turn_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE native_sessions (
                backend TEXT NOT NULL, thread_id TEXT NOT NULL, home_dir TEXT NOT NULL, isolated_home TEXT NOT NULL, driver TEXT NOT NULL,
                PRIMARY KEY (backend,thread_id)
            )"""
        )
        sessions = conn.execute(
            "SELECT DISTINCT backend,thread_id FROM task_runs WHERE thread_id IS NOT NULL"
        ).fetchall()
        for session in sessions:
            backend = session["backend"]
            if backend not in native_homes:
                raise ValueError(f"native home must be specified for historical backend {backend}")
            native_home = native_homes[backend].resolve()
            isolated_home = (isolated_homes or {}).get(backend, native_home).resolve()
            if not native_home.is_relative_to(isolated_home):
                raise ValueError(f"native home must be inside its isolated home for historical backend {backend}")
            conn.execute(
                "INSERT INTO native_sessions VALUES (?,?,?,?,?)",
                (
                    backend,
                    session["thread_id"],
                    str(native_home),
                    str(isolated_home),
                    {"codex": "codex", "claude": "claude-code"}[backend],
                ),
            )
        rows = conn.execute("SELECT task_id,task_json FROM task_runs").fetchall()
        for row in rows:
            task = json.loads(row["task_json"])
            task.update(
                kind=_kind(task),
                execution_override={},
                execution=None,
                memory={"read_domains": [], "write_domain": None},
            )
            conn.execute(
                "UPDATE task_runs SET task_json=? WHERE task_id=?",
                (json.dumps(task, ensure_ascii=False, separators=(",", ":")), row["task_id"]),
            )
        requests = conn.execute("SELECT task_id,request_json FROM subtask_requests").fetchall()
        for row in requests:
            request = SubtaskRequest.model_validate_json(row["request_json"])
            conn.execute(
                "UPDATE subtask_requests SET request_json=? WHERE task_id=?",
                (request.model_dump_json(), row["task_id"]),
            )
        turns = conn.execute(
            """INSERT INTO task_turns SELECT task_id,backend,thread_id,turn_id,created_at
               FROM task_runs WHERE thread_id IS NOT NULL AND turn_id IS NOT NULL AND coalesced_into IS NULL"""
        ).rowcount
        contexts = conn.execute("SELECT count(*) FROM agent_contexts").fetchone()[0]
        # Old native sessions may already contain native memories. New execution
        # starts a fresh session; the old references remain on historical tasks.
        conn.execute("UPDATE agent_contexts SET thread_id=NULL")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return {"tasks": len(rows), "contexts": contexts, "turns": turns}
    finally:
        conn.close()
