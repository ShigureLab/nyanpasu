from __future__ import annotations

import json
import sqlite3

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.migration import migrate_state
from nyanpasu.models import AgentContext, AgentTask, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore


def old_state(path, *, active=False):
    store = StateStore(path)
    task = AgentTask(
        task_id="review",
        action=TaskAction.RUN,
        context_key="github:owner/repo#1",
        prompt="review",
        execution=NyanpasuConfig().resolve_execution(),
        metadata={"plugin_id": "github_reviewer"},
    )
    store.record_task(task)
    context = AgentContext(
        context_key=task.context_key,
        thread_id="session",
        session_worktree=None,
        workspace_key=None,
        revision=None,
    )
    store.bind_task_execution(task.task_id, "session", "turn", context=context)
    if not active:
        store.mark_task_done(
            TaskRunResult(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                thread_id="session",
                turn_id="turn",
                final_message="done",
            )
        )
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE agent_contexts DROP COLUMN memory_key")
        conn.execute("DROP TABLE task_turns")
        conn.execute("DROP TABLE native_sessions")
        data = json.loads(conn.execute("SELECT task_json FROM task_runs").fetchone()[0])
        for key in ("kind", "execution", "execution_override", "memory"):
            data.pop(key)
        conn.execute("UPDATE task_runs SET task_json=?", (json.dumps(data),))
        conn.execute("PRAGMA user_version=0")


def test_explicit_migration_preserves_history_without_inventing_model_or_access(tmp_path):
    path = tmp_path / "state.db"
    old_state(path)
    with pytest.raises(ValueError, match="requires migration"):
        StateStore(path)
    assert migrate_state(path, native_homes={"codex": tmp_path / "original-codex"}) == {
        "tasks": 1,
        "contexts": 1,
        "turns": 1,
    }
    store = StateStore(path)
    task = store.task_request("review")
    assert task.kind == "github_reviewer.review"
    assert task.execution is None
    assert task.memory.read_domains == ()
    assert store.task_run("review").thread_id == "session"
    context = store.get_context(task.context_key)
    assert context is not None and context.thread_id is None
    assert [turn["turn_id"] for turn in store.task_turns("review")] == ["turn"]
    assert migrate_state(path, native_homes={"codex": tmp_path / "original-codex"}) == {
        "tasks": 0,
        "contexts": 0,
        "turns": 0,
    }


def test_migration_rejects_active_work_without_partial_schema_changes(tmp_path):
    path = tmp_path / "state.db"
    old_state(path, active=True)
    with pytest.raises(ValueError, match="drain tasks"):
        migrate_state(path, native_homes={"codex": tmp_path / "original-codex"})
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "memory_key" not in {row[1] for row in conn.execute("PRAGMA table_info(agent_contexts)")}


def test_migration_rolls_back_invalid_task_payload(tmp_path):
    path = tmp_path / "state.db"
    old_state(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE task_runs SET task_json='not json'")
    with pytest.raises(json.JSONDecodeError):
        migrate_state(path, native_homes={"codex": tmp_path / "original-codex"})
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "memory_key" not in {row[1] for row in conn.execute("PRAGMA table_info(agent_contexts)")}
