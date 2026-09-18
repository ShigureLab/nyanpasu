from __future__ import annotations

from pathlib import Path
from typing import Any

from nyanpasu.models import AgentContext, AgentTask, TaskAction, TaskRunResult, TaskStatus, WorkspaceRef
from nyanpasu.store import StateStore


def _task(
    task_id: str,
    *,
    context_key: str = "demo:1",
    metadata: dict[str, Any] | None = None,
) -> AgentTask:
    return AgentTask(
        task_id=task_id,
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
    assert store.enqueue_task(first, default_backend="codex", coalesce_since=0) == (True, None)
    assert store.enqueue_task(second, default_backend="claude", coalesce_since=0) == (True, None)
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
    task = _task("cleanup").model_copy(update={"action": TaskAction.CLEANUP})
    assert store.record_task(task, default_backend="claude")
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


def test_migrate_conversation_copies_preserves_task_and_execution_references(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    state = StateStore(path)
    for task_id in ("original", "alias"):
        state.record_task(AgentTask(task_id=task_id, context_key="demo", action=TaskAction.RUN, prompt="request"))
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE task_runs ADD COLUMN result_json TEXT")
        conn.execute("ALTER TABLE task_runs DROP COLUMN coalesced_into")
        conn.execute(
            "UPDATE task_runs SET result_json=? WHERE task_id='original'",
            ('{"final_message":"conversation copy","raw_events":[{"text":"duplicate"}]}',),
        )
        conn.execute("UPDATE task_runs SET result_json=? WHERE task_id='alias'", ('{"coalesced_into":"original"}',))
        conn.executescript("""
            CREATE TABLE transcript_tasks(task_id TEXT,session_id TEXT,turn_id TEXT);
            CREATE TABLE transcript_sessions(session_id TEXT,thread_id TEXT);
            CREATE TABLE transcript_events(content TEXT);
            INSERT INTO transcript_tasks VALUES('original','old-session','turn');
            INSERT INTO transcript_sessions VALUES('old-session','thread');
            INSERT INTO transcript_events VALUES('conversation copy');
        """)
    migrated = StateStore(path)
    assert next(task for task in migrated.recent_tasks() if task.task_id == "original").thread_id == "thread"
    assert next(task for task in migrated.recent_tasks() if task.task_id == "original").turn_id == "turn"
    assert migrated.coalesced_tasks_for("original")[0].task_id == "alias"
    StateStore(path)
    with sqlite3.connect(path) as conn:
        assert "result_json" not in {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'transcript_%'").fetchall()
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 2
