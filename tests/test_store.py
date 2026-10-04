from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.memory import MemoryAccess
from nyanpasu.models import AgentContext, AgentTask, TaskAction, TaskRunResult, TaskStatus, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride


def _task(
    task_id: str,
    *,
    context_key: str = "demo:1",
    metadata: dict[str, Any] | None = None,
) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        execution=NyanpasuConfig().resolve_execution(),
        action=TaskAction.RUN,
        context_key=context_key,
        prompt="do work",
        workspace=WorkspaceRef(
            key="owner/repo",
            local_path=Path("/repo"),
            remote="https://example.invalid/repo.git",
            ref="refs/heads/main",
            revision="abc",
        ),
        dedupe_key=task_id,
        metadata=metadata or {},
    )


def test_task_dedup_and_context_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    task = _task("task-1")

    assert store.record_task(task)
    assert not store.record_task(task)

    context = AgentContext(
        context_key="demo:1",
        thread_id="thread-1",
        session_worktree=tmp_path / "wt",
        workspace_key="owner/repo",
        revision="abc",
    )
    store.upsert_context(context)

    assert store.get_context("demo:1") == context
    assert store.list_contexts() == [context]
    assert store.delete_context("demo:1") == context
    assert store.get_context("demo:1") is None


def test_failed_task_is_still_deduplicated(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    task = _task("task-1")

    assert store.record_task(task)
    store.mark_task_failed("task-1", "boom")

    assert not store.record_task(task)
    assert store.task_status("task-1") == "failed"


def test_active_task_and_coalesced_task(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    first = _task("task-1").model_copy(update={"coalesce_key": "batch"})
    second = _task("task-2").model_copy(update={"coalesce_key": "batch"})

    assert store.record_task(first)
    assert store.enqueue_task(second, coalesce_since=0) == (True, first.task_id)

    active = store.active_task_for_context("demo:1", exclude_task_id="task-2")
    assert active is not None
    assert active.task_id == "task-1"

    assert store.task_status("task-2") == "completed"
    coalesced = store.coalesced_tasks_for("task-1")
    assert len(coalesced) == 1
    assert coalesced[0].task_id == "task-2"
    assert coalesced[0].task["context_key"] == "demo:1"


def test_active_task_can_filter_statuses(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    task = _task("task-1")

    assert store.record_task(task)
    store.mark_task_running("task-1", None)

    assert store.active_task_for_context("demo:1", statuses=("queued",)) is None
    active = store.active_task_for_context("demo:1", statuses=("running",))
    assert active is not None
    assert active.task_id == "task-1"


def test_tasks_for_different_backends_do_not_coalesce(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    first = _task("task-1").model_copy(update={"coalesce_key": "batch"})
    second = _task("task-2").model_copy(update={"coalesce_key": "batch"})
    assert store.enqueue_task(first, coalesce_since=0) == (True, None)
    second = second.model_copy(
        update={"execution": NyanpasuConfig().resolve_execution(override=ExecutionOverride(backend="claude"))}
    )
    assert store.enqueue_task(second, coalesce_since=0) == (True, None)
    assert {task.status for task in store.recent_tasks()} == {TaskStatus.QUEUED}
    assert store.task_backend("task-1") == "codex"
    assert store.task_backend("task-2") == "claude"


def test_cleanup_keeps_context_backend_after_configuration_switch(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.upsert_context(
        AgentContext(
            context_key="demo:1",
            backend="codex",
            thread_id="old-session",
            session_worktree=None,
            workspace_key=None,
            revision=None,
        )
    )
    task = _task("cleanup").model_copy(
        update={
            "action": TaskAction.CLEANUP,
            "execution": NyanpasuConfig().resolve_execution(override=ExecutionOverride(backend="claude")),
        }
    )
    assert store.record_task(task)
    assert store.task_backend("cleanup") == "codex"


def test_mark_task_done_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    task = _task("task-1")
    assert store.record_task(task)

    store.mark_task_done(
        TaskRunResult(
            task_id="task-1",
            status=TaskStatus.COMPLETED,
            thread_id="thread-1",
            turn_id="turn-1",
            final_message="done",
            event_worktree=tmp_path / "event",
            session_worktree=tmp_path / "session",
        )
    )

    recent = store.recent_tasks()
    assert recent[0].task_id == "task-1"
    assert recent[0].status is TaskStatus.COMPLETED
    assert recent[0].thread_id == "thread-1"


def test_mark_task_interrupted_keeps_dedupe_and_requeues(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    task = _task("task-1")
    assert store.record_task(task)
    store.mark_task_running("task-1", None)

    store.mark_task_interrupted("task-1", "shutdown")

    assert store.task_status("task-1") == "queued"
    assert not store.record_task(task)
    recent = store.recent_tasks()
    assert recent[0].task_id == "task-1"
    assert recent[0].error == "shutdown"


def test_context_lease_acquire_heartbeat_release_and_expiry(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")

    assert store.try_acquire_context_lease("demo:1", owner_id="owner-1", task_id="task-1", ttl_seconds=60)
    assert not store.try_acquire_context_lease("demo:1", owner_id="owner-2", task_id="task-2", ttl_seconds=60)

    lease = store.get_context_lease("demo:1")
    assert lease is not None
    assert lease.owner_id == "owner-1"
    assert lease.task_id == "task-1"

    assert store.heartbeat_context_lease("demo:1", owner_id="owner-1", task_id="task-1", ttl_seconds=60)
    assert not store.heartbeat_context_lease("demo:1", owner_id="owner-2", task_id="task-2", ttl_seconds=60)
    assert store.release_context_lease("demo:1", owner_id="owner-1", task_id="task-1")
    assert store.get_context_lease("demo:1") is None

    assert store.try_acquire_context_lease("demo:1", owner_id="owner-1", task_id="task-1", ttl_seconds=-1)
    assert store.try_acquire_context_lease("demo:1", owner_id="owner-2", task_id="task-2", ttl_seconds=60)
    stolen = store.get_context_lease("demo:1")
    assert stolen is not None
    assert stolen.owner_id == "owner-2"
    assert stolen.task_id == "task-2"


def test_release_context_leases_for_owner(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    assert store.try_acquire_context_lease("demo:1", owner_id="owner-1", task_id="task-1", ttl_seconds=60)
    assert store.try_acquire_context_lease("demo:2", owner_id="owner-1", task_id="task-2", ttl_seconds=60)
    assert store.try_acquire_context_lease("demo:3", owner_id="owner-2", task_id="task-3", ttl_seconds=60)

    assert store.release_context_leases_for_owner("owner-1") == 2

    assert store.get_context_lease("demo:1") is None
    assert store.get_context_lease("demo:2") is None
    assert store.get_context_lease("demo:3") is not None


def test_dashboard_snapshot_groups_plugin_backlog_and_recent_tasks(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    queued = _task(
        "task-1",
        context_key="repo:queued",
        metadata={
            "plugin_id": "github_pr_maker",
            "request": {"repo": "owner/repo", "title": "Implement dashboard"},
        },
    )
    running = _task(
        "task-2",
        context_key="repo:running",
        metadata={
            "plugin_id": "github_reviewer",
            "pull_request": {"repo": "owner/repo", "number": 12},
            "github_event": "pull_request",
            "review_mode": "followup_review",
        },
    )
    failed = _task("task-3", context_key="repo:failed", metadata={"plugin_id": "github_reviewer"})
    done = _task("task-4", context_key="repo:done")

    assert store.record_task(queued)
    assert store.record_task(running)
    assert store.record_task(failed)
    assert store.record_task(done)
    store.mark_task_running("task-2", None)
    store.mark_task_failed("task-3", "boom")
    store.mark_task_done(
        TaskRunResult(
            task_id="task-4",
            status=TaskStatus.COMPLETED,
            thread_id="thread-1",
            turn_id="turn-1",
            final_message="done",
        )
    )
    store.upsert_context(
        AgentContext(
            context_key="repo:running",
            thread_id="thread-1",
            session_worktree=tmp_path / "wt",
            workspace_key="owner/repo",
            revision="abc",
        )
    )
    assert store.try_acquire_context_lease("repo:running", owner_id="owner", task_id="task-2", ttl_seconds=60)

    snapshot = store.dashboard_snapshot()

    assert snapshot.totals.total == 4
    assert snapshot.totals.queued == 1
    assert snapshot.totals.running == 1
    assert snapshot.totals.failed == 1
    assert snapshot.totals.completed == 1
    assert snapshot.totals.backlog == 2
    assert snapshot.totals.contexts == 1
    assert snapshot.totals.active_leases == 1
    assert snapshot.backlog[0].task_id == "task-2"
    assert snapshot.backlog[0].status == "running"
    assert snapshot.backlog[1].title == "Implement dashboard"
    assert snapshot.backlog[1].source == "owner/repo"
    plugins = {plugin.plugin_id: plugin for plugin in snapshot.plugins}
    assert plugins["github_reviewer"].total == 2
    assert plugins["github_reviewer"].running == 1
    assert plugins["github_reviewer"].failed == 1
    assert plugins["github_pr_maker"].queued == 1
    assert plugins["core"].completed == 1


def test_concurrent_enqueues_merge_directly_without_chains(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = StateStore(tmp_path / "state.db")
    first = _task("first").model_copy(update={"coalesce_key": "batch"})
    store.record_task(first)
    incoming = [_task(task_id).model_copy(update={"coalesce_key": "batch"}) for task_id in ("second", "third")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.enqueue_task, task, coalesce_since=0) for task in incoming]
        assert all(future.result() == (True, "first") for future in futures)
    assert {item.task_id for item in store.coalesced_tasks_for("first")} == {"second", "third"}


def test_claimed_tasks_and_cleanup_are_merge_boundaries(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    first = _task("first").model_copy(update={"coalesce_key": "batch"})
    store.record_task(first)
    store.mark_task_running(first.task_id, None)
    second = _task("second").model_copy(update={"coalesce_key": "batch"})
    assert store.enqueue_task(second, coalesce_since=0) == (True, None)
    cleanup = _task("cleanup").model_copy(update={"action": TaskAction.CLEANUP})
    store.record_task(cleanup)
    third = _task("third").model_copy(update={"coalesce_key": "batch"})
    assert store.enqueue_task(third, coalesce_since=0) == (True, None)
    assert store.coalesced_tasks_for("first") == []
    assert store.coalesced_tasks_for("second") == []


def test_distinct_merge_keys_keep_tasks_separate_in_shared_context(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    first = _task("first").model_copy(update={"coalesce_key": "pr-1"})
    second = _task("second").model_copy(update={"coalesce_key": "pr-2"})
    store.record_task(first)

    assert store.enqueue_task(second, coalesce_since=0) == (True, None)
    assert store.coalesced_tasks_for("first") == []


def test_store_rejects_unresolved_tasks_before_admission(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    task = _task("unresolved").model_copy(update={"execution": None})
    with pytest.raises(ValueError, match="execution must be resolved before admission"):
        store.record_task(task)
    with pytest.raises(ValueError, match="execution must be resolved before admission"):
        store.enqueue_task(task, coalesce_since=0)
    assert store.recent_tasks() == []


def test_unsupported_conversation_schema_is_not_modified(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    state = StateStore(path)
    state.record_task(_task("original"))
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE task_runs ADD COLUMN result_json TEXT")
        conn.execute("ALTER TABLE task_runs DROP COLUMN coalesced_into")
        conn.execute("PRAGMA user_version=0")
        conn.execute(
            "UPDATE task_runs SET result_json=? WHERE task_id='original'",
            ('{"final_message":"conversation copy","raw_events":[{"text":"duplicate"}]}',),
        )
        conn.executescript("""
            CREATE TABLE transcript_events(content TEXT);
            INSERT INTO transcript_events VALUES('conversation copy');
        """)
    with pytest.raises(ValueError, match="Unsupported state schema version"):
        StateStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "result_json" in {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
        assert conn.execute("SELECT content FROM transcript_events").fetchall() == [("conversation copy",)]
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1


def test_upgrade_preserves_existing_tasks_and_turns_and_records_immutable_memory_receipts(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    state = StateStore(path)
    original = _task("original")
    state.record_task(original)
    state.bind_task_execution(original.task_id, "thread", "old-turn")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE task_turns DROP COLUMN memory_context_json")
        conn.execute("PRAGMA user_version=1")
        before = conn.execute("SELECT * FROM task_turns").fetchall()

    migrated = StateStore(path)
    assert migrated.task_request(original.task_id) == original
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT task_id,backend,thread_id,turn_id,started_at FROM task_turns").fetchall() == before
        assert conn.execute("SELECT memory_context_json FROM task_turns").fetchall() == [(None,)]

    receipt = {"prompt": "Historical evidence: 修复已验证", "bytes": 42, "selected": [], "skipped": []}
    migrated.bind_task_execution(original.task_id, "thread", "new-turn", memory_context=receipt)
    migrated.bind_task_execution(original.task_id, "thread", "new-turn", memory_context={"prompt": "changed"})
    reopened = StateStore(path)
    turns = reopened.task_turns(original.task_id)
    assert len(turns) == 2
    assert json.loads(turns[1]["memory_context_json"]) == receipt
    assert turns[0]["memory_context_json"] is None


def test_source_contexts_use_execution_order_and_isolate_contribution_audiences(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    for identity, domain in (("queued-first", "public"), ("executed-first", "public"), ("hidden", "private:bob")):
        state.record_task(_task(identity).model_copy(update={"memory": MemoryAccess((domain,), domain)}))
        state.bind_task_execution(identity, "thread", identity)
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("UPDATE task_turns SET started_at=20 WHERE task_id='queued-first'")
        conn.execute("UPDATE task_turns SET started_at=10 WHERE task_id='executed-first'")
    assert state.memory_source_contexts("public") == {
        "queued-first": ("demo:1", 1, 20),
        "executed-first": ("demo:1", 1, 10),
    }
