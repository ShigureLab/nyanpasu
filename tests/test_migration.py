from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from typer.testing import CliRunner

from nyanpasu.__main__ import app
from nyanpasu.config import NyanpasuConfig
from nyanpasu.migration import migrate_state
from nyanpasu.models import AgentContext, AgentTask, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.transcript.claude import ClaudeHistorySource


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
    location = store.native_home("codex", "session")
    assert location.isolated_home == location.native_home == tmp_path / "original-codex"
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


def test_migration_maps_codewiz_history_to_its_actual_native_storage(tmp_path):
    path = tmp_path / "state.db"
    old_state(path)
    session_id = "10000000-0000-4000-8000-000000000001"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE task_runs SET backend='claude',thread_id=?", (session_id,))
    native = tmp_path / "operator" / ".claude"
    project = native / "projects" / "repo"
    project.mkdir(parents=True)
    (project / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "uuid": "historical-message",
                "type": "user",
                "message": {"role": "user", "content": "preserved native conversation"},
            }
        )
        + "\n"
    )
    wrapper = tmp_path / "operator" / ".cc-mirror" / "codewiz-cc" / "config"
    wrapper.mkdir(parents=True)
    (wrapper / "projects").symlink_to(native / "projects", target_is_directory=True)
    migrate_state(path, native_homes={"claude": native}, isolated_homes={"claude": native})
    location = StateStore(path).native_home("claude", session_id)
    assert location.native_home == location.isolated_home == native

    source = ClaudeHistorySource({"CLAUDE_CONFIG_DIR": str(location.native_home)}, root=location.isolated_home)
    history = asyncio.run(source.read_session(session_id))

    assert history.metadata.id == session_id
    assert "preserved native conversation" in history.model_dump_json()
    assert (wrapper / "projects").is_symlink()


def test_migration_rejects_native_home_outside_explicit_reader_root(tmp_path):
    path = tmp_path / "state.db"
    old_state(path)
    with pytest.raises(ValueError, match="inside its isolated home"):
        migrate_state(path, native_homes={"codex": tmp_path / "native"}, isolated_homes={"codex": tmp_path / "other"})
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_migration_cli_uses_explicit_paths_without_loading_execution_config(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    old_state(path)
    native = tmp_path / "native"
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "unrelated-new-home"))
    monkeypatch.setenv("NYANPASU_CLAUDE_BIN", "obsolete-environment-must-not-affect-migration")
    result = CliRunner().invoke(
        app,
        [
            "migrate-state",
            str(path),
            "--native-home",
            f"codex={native}",
            "--isolated-home",
            f"codex={native}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["tasks"] == 1
    assert StateStore(path).native_home("codex", "session").isolated_home == native
