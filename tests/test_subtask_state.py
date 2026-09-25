from __future__ import annotations

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
