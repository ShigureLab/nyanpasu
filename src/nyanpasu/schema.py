from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
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
    PRIMARY KEY (task_id, backend, thread_id, turn_id)
);
CREATE TABLE native_sessions (
    backend TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    home_dir TEXT NOT NULL, isolated_home TEXT NOT NULL, driver TEXT NOT NULL,
    PRIMARY KEY (backend, thread_id)
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


def initialize(conn: sqlite3.Connection) -> None:
    """Create new state; existing state must be explicitly migrated before serving."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone()
    if exists:
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("State schema requires migration; stop the service and run nyanpasu migrate-state")
        return
    conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
