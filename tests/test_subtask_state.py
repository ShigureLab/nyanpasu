from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus, WorkspaceRef
from nyanpasu.store import StateStore


def root(store: StateStore, name: str = "review") -> AgentTask:
    task = AgentTask(task_id=name, context_key="pr:1", action=TaskAction.RUN, prompt="Review")
    store.record_task(task)
    store.mark_task_running(name, None)
    return store.task_request(name)


def request(key: str = "design") -> SubtaskRequest:
    return SubtaskRequest(request_key=key, prompt="Design from the requirements", developer_instructions="Fresh input")


def finish(store: StateStore, task: AgentTask) -> None:
    store.mark_task_done(
        TaskRunResult(
            task_id=task.task_id, status=TaskStatus.COMPLETED, thread_id="child", turn_id="turn", final_message=""
        )
    )


def test_concurrent_retries_create_one_child_and_preserve_input(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = root(store)
    with ThreadPoolExecutor(max_workers=4) as workers:
        children = list(workers.map(lambda _: store.create_subtask(parent.task_id, request()), range(8)))
    assert len({child.task_id for child in children}) == 1
    child = children[0]
    assert child.spawned_by_task_id == parent.task_id
    assert child.developer_instructions == "Fresh input"
    assert child.prompt == "Design from the requirements"
    assert store.root_task_id(child.task_id) == parent.task_id
    assert store.context_scope(child.context_key).parent_context_key == parent.context_key
    with pytest.raises(ValueError, match="different input"):
        store.create_subtask(parent.task_id, request().model_copy(update={"prompt": "Changed requirements"}))


@pytest.mark.parametrize("legacy", [False, True])
def test_request_keys_belong_to_each_parent_and_survive_migration(tmp_path, legacy):
    path = tmp_path / "state.db"
    store = StateStore(path)
    first = root(store)
    child = store.create_subtask(first.task_id, request())
    if legacy:
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                ALTER TABLE subtask_requests RENAME TO current_requests;
                CREATE TABLE subtask_requests (
                    parent_context_key TEXT NOT NULL, parent_generation INTEGER NOT NULL,
                    request_key TEXT NOT NULL, request_json TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (parent_context_key, parent_generation, request_key)
                );
                INSERT INTO subtask_requests
                    SELECT p.context_key,p.context_generation,s.request_key,s.request_json,s.task_id
                    FROM current_requests s JOIN task_runs p ON p.task_id=s.parent_task_id;
                DROP TABLE current_requests;
            """)
    store = StateStore(path)
    assert store.create_subtask(first.task_id, request()) == child
    finish(store, child)
    finish(store, first)
    second = root(store, "followup")
    assert second.context_generation == first.context_generation
    next_child = store.create_subtask(second.task_id, request())
    assert next_child.task_id != child.task_id
    assert next_child.spawned_by_task_id == second.task_id
    store.wait_for_subtasks(second.task_id, [next_child.task_id])
    assert not store.wait_is_ready(second.task_id)


def test_concurrent_preparations_deduplicate_original_input(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = root(store)
    original = request()
    with ThreadPoolExecutor(max_workers=4) as workers:
        children = list(
            workers.map(
                lambda i: store.create_subtask(
                    parent.task_id, original, prepared=original.model_copy(update={"inputs": {"target": i}})
                ),
                range(8),
            )
        )
    assert len({child.task_id for child in children}) == 1
    reopened = StateStore(store.db_path)
    assert reopened.existing_subtask(parent.task_id, original) == children[0]
    assert len(reopened.subtasks(parent.task_id)) == 1


@pytest.mark.parametrize("finish_before_wait", [False, True])
def test_wait_survives_restart_and_observes_child_finishing_on_either_side(tmp_path, finish_before_wait):
    path = tmp_path / "state.db"
    store = StateStore(path)
    parent = root(store)
    child = store.create_subtask(parent.task_id, request())
    if finish_before_wait:
        finish(store, child)
    store.wait_for_subtasks(parent.task_id, [child.task_id])
    store.mark_task_waiting(parent.task_id)
    resumed = StateStore(path)
    assert resumed.wait_is_ready(parent.task_id) is finish_before_wait
    if not finish_before_wait:
        finish(resumed, child)
    assert resumed.wait_is_ready(parent.task_id)
    assert [task.task_id for task in resumed.unfinished_roots()] == [parent.task_id]
    assert resumed.resume_subtasks(parent.task_id)[0].task_id == child.task_id
    assert resumed.task_status(parent.task_id) == "queued"
    assert resumed.waiting_for(parent.task_id) == []
    assert resumed.resume_subtasks(parent.task_id) == []


def test_cannot_wait_on_other_roots_or_an_ancestor(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = root(store)
    child = store.create_subtask(parent.task_id, request())
    store.mark_task_running(child.task_id, None)
    unrelated = AgentTask(task_id="other", context_key="pr:2", action=TaskAction.RUN, prompt="Other")
    store.record_task(unrelated)
    with pytest.raises(ValueError, match="descendant"):
        store.wait_for_subtasks(child.task_id, [parent.task_id])
    with pytest.raises(ValueError, match="descendant"):
        store.wait_for_subtasks(parent.task_id, [unrelated.task_id])


def test_closing_tree_rejects_creation_and_recovery_and_reopen_fences_old_results(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = root(store)
    child = store.create_subtask(parent.task_id, request())
    store.mark_task_running(child.task_id, None)
    grandchild = store.create_subtask(child.task_id, request("test"))
    scopes = store.begin_context_cleanup(parent.context_key)
    assert {scope.context_key for scope in scopes} == {parent.context_key, child.context_key, grandchild.context_key}
    with pytest.raises(ValueError, match="closing"):
        store.create_subtask(child.task_id, request("late"))
    assert store.unfinished_roots() == []
    for scope in reversed(scopes):
        store.close_context_scope(scope.context_key, scope.generation)
    reopened = root(store, "reopened")
    assert reopened.context_generation == parent.context_generation + 1
    assert store.task_is_active(child.task_id) is False
    with pytest.raises(ValueError, match="inactive"):
        store.record_subtask_result(child.task_id, {"summary": "late", "artifacts": []})
    finish(store, child)
    assert store.task_status(child.task_id) == "cancelled"
    store.close_context_scope(parent.context_key, parent.context_generation)
    assert store.context_scope(parent.context_key).lifecycle == "active"


def test_creation_racing_cleanup_never_leaves_an_active_orphan(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = root(store)
    with ThreadPoolExecutor(max_workers=2) as workers:
        creation = workers.submit(store.create_subtask, parent.task_id, request())
        cleanup = workers.submit(store.begin_context_cleanup, parent.context_key)
        cleanup.result()
        try:
            child = creation.result()
        except ValueError:
            pass
        else:
            assert store.context_scope(child.context_key).lifecycle == "closing"
            assert store.task_is_active(child.task_id) is False


def test_reloaded_subtasks_remain_separate_from_coalesced_events(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = root(store)
    child = store.create_subtask(parent.task_id, request())
    reloaded = StateStore(tmp_path / "state.db")
    assert reloaded.task_request(parent.task_id).spawned_by_task_id is None
    assert [item.task_id for item in reloaded.subtasks(parent.task_id)] == [child.task_id]
    assert reloaded.coalesced_tasks_for(parent.task_id) == []


@pytest.mark.parametrize("failed_owner", ["root", "child"])
def test_failure_and_descendant_cancellation_commit_or_rollback_together(tmp_path, failed_owner):
    path = tmp_path / "state.db"
    store = StateStore(path)
    parent = root(store)
    child = store.create_subtask(parent.task_id, request())
    store.mark_task_running(child.task_id, None)
    pending = store.create_subtask(child.task_id, request("pending"))
    completed = store.create_subtask(child.task_id, request("completed"))
    finish(store, completed)
    sibling = store.create_subtask(parent.task_id, request("sibling"))
    owner = parent if failed_owner == "root" else child
    # Failure halfway through persistence must not leave a terminal owner behind.
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TRIGGER reject_cancel BEFORE UPDATE OF status ON task_runs
            WHEN NEW.status = 'cancelled'
            BEGIN SELECT RAISE(ABORT, 'interrupted cancellation'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="interrupted cancellation"):
        store.mark_task_failed(owner.task_id, "backend failed")
    reloaded = StateStore(path)
    assert reloaded.task_status(owner.task_id) == "running"
    assert reloaded.task_status(pending.task_id) == "queued"
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER reject_cancel")
    reloaded.mark_task_failed(owner.task_id, "backend failed")
    reloaded = StateStore(path)
    assert reloaded.task_status(owner.task_id) == "failed"
    assert reloaded.task_status(pending.task_id) == "cancelled"
    assert reloaded.task_status(completed.task_id) == "completed"
    assert reloaded.task_status(sibling.task_id) == ("cancelled" if failed_owner == "root" else "queued")
    assert {task.task_id for task in reloaded.unfinished_tasks()} == (
        set() if failed_owner == "root" else {parent.task_id, sibling.task_id}
    )


def test_child_workspace_and_wait_roundtrip_preserve_pinned_revision(tmp_path):
    store = StateStore(tmp_path / "state.db")
    parent = AgentTask(
        task_id="root",
        context_key="pr:1",
        action=TaskAction.RUN,
        prompt="Review",
        workspace=WorkspaceRef(key="repo", local_path=tmp_path, revision="head"),
    )
    store.record_task(parent)
    store.mark_task_running(parent.task_id, None)
    spec = request().model_copy(update={"revision": "base"})
    child = store.create_subtask(parent.task_id, spec)
    assert store.create_subtask(parent.task_id, spec) == child
    assert child.workspace is not None
    assert child.workspace.local_path == tmp_path
    assert child.workspace.revision == "base"
    finish(store, child)
    store.wait_for_subtasks(parent.task_id, [child.task_id])
    store.resume_subtasks(parent.task_id)
    assert store.task_request(parent.task_id).workspace == parent.workspace


def test_old_cleanup_does_not_close_reopened_context(tmp_path):
    store = StateStore(tmp_path / "state.db")
    original = root(store)
    store.begin_context_cleanup(original.context_key, original.context_generation)
    store.close_context_scope(original.context_key, original.context_generation)
    reopened = root(store, "reopened")
    assert store.begin_context_cleanup(original.context_key, original.context_generation) == []
    assert store.task_is_active(reopened.task_id)


def test_ignored_event_after_cleanup_finishes_without_reopening_context(tmp_path):
    store = StateStore(tmp_path / "state.db")
    original = root(store)
    store.begin_context_cleanup(original.context_key)
    store.close_context_scope(original.context_key, original.context_generation)
    ignored = original.model_copy(update={"task_id": "ignored", "action": TaskAction.IGNORED})
    store.record_task(ignored)
    finish(store, ignored)
    assert store.task_status(ignored.task_id) == "completed"
    assert store.context_scope(original.context_key).lifecycle == "closed"
    assert store.unfinished_tasks() == []
