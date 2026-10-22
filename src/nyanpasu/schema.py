from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

SCHEMA_VERSION = 5

SUBTASK_DELIVERIES = """
CREATE TABLE subtask_deliveries (
    parent_task_id TEXT NOT NULL,
    child_task_id TEXT NOT NULL,
    consumed_at REAL,
    PRIMARY KEY (parent_task_id, child_task_id)
)
"""

SCHEMA = (
    """
CREATE TABLE agent_contexts (
    context_key TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    thread_id TEXT,
    session_worktree TEXT,
    workspace_key TEXT,
    revision TEXT,
    memory_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE task_runs (
    task_id TEXT PRIMARY KEY,
    dedupe_key TEXT,
    context_key TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    backend TEXT NOT NULL,
    event_worktree TEXT,
    thread_id TEXT,
    turn_id TEXT,
    task_json TEXT NOT NULL,
    coalesced_into TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    spawned_by_task_id TEXT,
    context_generation INTEGER NOT NULL DEFAULT 1,
    wait_for TEXT,
    wait_mode TEXT NOT NULL DEFAULT 'all',
    subtask_result TEXT
);
CREATE UNIQUE INDEX idx_task_runs_dedupe_key ON task_runs(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX idx_task_runs_spawned_by ON task_runs(spawned_by_task_id);
CREATE TABLE task_turns (
    task_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    memory_context_json TEXT,
    developer_instructions TEXT,
    PRIMARY KEY (task_id, backend, thread_id, turn_id)
);
CREATE TABLE context_leases (
    context_key TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE context_scopes (
    context_key TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'active',
    parent_context_key TEXT,
    parent_generation INTEGER
);
CREATE TABLE subtask_requests (
    parent_task_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_json TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (parent_task_id, request_key)
);
"""
    + SUBTASK_DELIVERIES
    + ";"
)


def initialize(conn: sqlite3.Connection) -> None:
    """Create state or atomically upgrade earlier schemas."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone()
        if exists:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                conn.execute("ALTER TABLE task_turns ADD COLUMN memory_context_json TEXT")
                version = 2
            if version == 2:
                conn.execute("ALTER TABLE task_turns ADD COLUMN developer_instructions TEXT")
                version = 3
            if version == 3:
                conn.execute("DROP TABLE IF EXISTS native_sessions")
                version = 4
            if version == 4:
                conn.execute("ALTER TABLE task_runs ADD COLUMN wait_mode TEXT NOT NULL DEFAULT 'all'")
                conn.execute(SUBTASK_DELIVERIES)
                # A previously queued continuation may not yet have received its results.
                conn.execute("""INSERT INTO subtask_deliveries (parent_task_id, child_task_id)
                    SELECT r.task_id, child.value FROM task_runs r,
                    json_each(r.task_json, '$.metadata.resumed_subtasks') child
                    WHERE r.status IN ('queued', 'running', 'waiting')""")
                conn.execute("""UPDATE task_runs SET task_json=json_remove(task_json, '$.metadata.resumed_subtasks')
                    WHERE status IN ('queued', 'running', 'waiting')""")
                version = 5
            if version != SCHEMA_VERSION:
                raise ValueError(f"Unsupported state schema version {version}; expected {SCHEMA_VERSION}")
        else:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
