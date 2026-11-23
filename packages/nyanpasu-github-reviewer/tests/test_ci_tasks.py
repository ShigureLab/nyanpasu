from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentContext, AgentTask, SubtaskRequest, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride
from nyanpasu_github_reviewer.ci import CIFailure, CISnapshot
from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PullRequestRef,
    RepoSettings,
    ReviewAction,
    ReviewEvent,
)
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin

HEAD = "a" * 40
NEXT_HEAD = "b" * 40
plugin_module = importlib.import_module("nyanpasu_github_reviewer.plugin")


def snapshot(head=HEAD, *, failed=True, attempt=1):
    failures = (
        (
            CIFailure(
                id=f"check:10:attempt:{attempt}",
                name="unit / Python 3.12",
                conclusion="failure",
                url="https://github.com/owner/repo/actions/runs/7/job/10",
                kind="check",
                run_id=7,
                run_attempt=attempt,
                tested_sha=head,
            ),
        )
        if failed
        else ()
    )
    return CISnapshot(head_sha=head, failures=failures)


@pytest.fixture
def ci_mocks(monkeypatch):
    mocks = {
        "fetch_ci_snapshot": Mock(return_value=snapshot()),
        "_has_skip_review_label": Mock(return_value=False),
        "read_review_dashboard": Mock(return_value=None),
        "apply_ci_patch": Mock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(plugin_module, name, mock)
    return mocks


@pytest.fixture
def ci_plugin(tmp_path, monkeypatch, ci_mocks):
    config = NyanpasuConfig.model_validate(
        {
            "state_dir": str(tmp_path),
            "tasks": {
                "kinds": {
                    "github_reviewer.review": {"execution": {"backend": "codex", "model": "review-model"}},
                    "github_reviewer.ci-analysis": {
                        "execution": {
                            "backend": "claude",
                            "model": "deepseek-v4.1-flash-ali",
                            "reasoning": "medium",
                        },
                        "limits": {"turn_timeout_seconds": 600},
                    },
                }
            },
        }
    )
    plugin = GitHubReviewerPlugin(
        GitHubReviewerConfig(repos={"owner/repo": RepoSettings(local_path=tmp_path / "repo")})
    )
    plugin.runtime = Mock(config=config)
    plugin.state_store = StateStore(config.db_path)
    pr = PullRequestRef(
        repo="owner/repo",
        number=1,
        url="https://github.com/owner/repo/pull/1",
        base_ref="main",
        head_ref="feature",
        head_sha=HEAD,
        state="open",
        draft=False,
    )
    monkeypatch.setattr(plugin_module, "_fetch_pr", lambda *args: pr)
    return plugin, pr


def event_task(plugin, pr, trigger="ci_changed", task_id="ci-1"):
    event = ReviewEvent(
        delivery_id=task_id,
        github_event="ci" if trigger == "ci_changed" else "pull_request",
        action=ReviewAction.REVIEW,
        pr=pr,
        after_sha=pr.head_sha,
        raw={"nyanpasu": {"trigger": trigger}},
    )
    task = plugin.event_to_task(event)
    return task.model_copy(update={"execution": plugin.runtime.config.resolve_execution(task.kind)})


@pytest.mark.anyio
@pytest.mark.parametrize("failed", [False, True])
async def test_ci_followup_refreshes_current_state_without_building_review_inventory(ci_plugin, monkeypatch, failed):
    plugin, pr = ci_plugin
    queued = event_task(plugin, pr)
    current = snapshot(NEXT_HEAD, failed=failed)
    monkeypatch.setattr(plugin_module, "_fetch_pr", lambda *args: pr.model_copy(update={"head_sha": NEXT_HEAD}))
    monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", lambda *args: current)
    inventory = Mock(side_effect=AssertionError("CI-only followup must not rebuild the review inventory"))
    monkeypatch.setattr(plugin_module, "build_inventory", inventory)
    context = AgentContext(
        context_key=queued.context_key,
        thread_id="original-review",
        session_worktree=None,
        workspace_key=pr.repo,
        revision=HEAD,
    )

    prepared = await plugin.prepare_task(queued, (), context)

    assert prepared.action is (TaskAction.RUN if failed else TaskAction.IGNORED)
    assert prepared.context_key == queued.context_key == "github:owner/repo#1"
    assert prepared.kind == "github_reviewer.review"
    assert prepared.execution.model == "review-model"
    assert prepared.workspace.revision == NEXT_HEAD
    assert prepared.metadata["ci_snapshot"]["fingerprint"] == current.fingerprint
    assert bool(prepared.metadata["ci_snapshot"]["failures"]) is failed
    assert "review_inventory" not in prepared.metadata
    inventory.assert_not_called()


def existing_dashboard(observed: CISnapshot, *, status: str = "completed") -> dict:
    return {
        "revision": 42,
        "url": "https://github.com/owner/repo/pull/1#issuecomment-1",
        "data": {
            "status": "approved",
            "source": {"head_sha": HEAD},
            "ci": {
                "source": {"head_sha": observed.head_sha},
                "snapshot_fingerprint": observed.fingerprint,
                "status": status,
                "failures": {
                    failure.id: {
                        "name": failure.name,
                        "url": failure.url,
                        "conclusion": failure.conclusion,
                        "tested_sha": failure.tested_sha,
                        "run_id": failure.run_id,
                        "run_attempt": failure.run_attempt,
                        "summary": "Existing diagnosis",
                        "evidence": "Verified job log",
                        "next_step": "Inspect configuration",
                    }
                    for failure in observed.failures
                },
            },
        },
    }


@pytest.mark.anyio
async def test_recovered_ci_only_removes_ci_field_with_observed_revision(ci_plugin, ci_mocks, monkeypatch):
    plugin, pr = ci_plugin
    queued = event_task(plugin, pr)
    recovered = snapshot(NEXT_HEAD, failed=False)
    current = pr.model_copy(update={"head_sha": NEXT_HEAD})
    monkeypatch.setattr(plugin_module, "_fetch_pr", Mock(return_value=current))
    ci_mocks["fetch_ci_snapshot"].return_value = recovered
    dashboard = existing_dashboard(snapshot())
    ci_mocks["read_review_dashboard"].return_value = dashboard

    prepared = await plugin.prepare_task(queued, (), None)

    assert prepared.action is TaskAction.IGNORED
    patch = [{"op": "remove", "path": "/ci"}]
    preview, publication = ci_mocks["apply_ci_patch"].call_args_list
    assert preview.args == publication.args == (plugin.config, current, dashboard["revision"], patch)
    assert preview.kwargs == {"preview": True}
    assert not publication.kwargs
    assert ci_mocks["fetch_ci_snapshot"].call_count == 2
    assert ci_mocks["_has_skip_review_label"].call_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["completed", "analyzing", "incomplete"])
async def test_ci_unchanged_failure_instances_finish_without_model_or_write(ci_plugin, ci_mocks, status):
    plugin, pr = ci_plugin
    ci_mocks["read_review_dashboard"].return_value = existing_dashboard(snapshot(), status=status)

    prepared = await plugin.prepare_task(event_task(plugin, pr), (), None)

    assert prepared.action is TaskAction.IGNORED
    ci_mocks["apply_ci_patch"].assert_not_called()


@pytest.mark.anyio
async def test_ci_recovery_of_one_job_reuses_remaining_explanation(ci_plugin, ci_mocks):
    plugin, pr = ci_plugin
    observed = snapshot()
    prior = observed.model_copy(update={"failures": (*observed.failures, *snapshot(attempt=2).failures)})
    dashboard = existing_dashboard(prior)
    ci_mocks["read_review_dashboard"].return_value = dashboard

    prepared = await plugin.prepare_task(event_task(plugin, pr), (), None)

    assert prepared.action is TaskAction.IGNORED
    publication = ci_mocks["apply_ci_patch"].call_args_list[-1]
    current_ci = publication.args[3][0]
    assert current_ci["op"] == "replace"
    assert current_ci["path"] == "/ci"
    assert current_ci["value"]["snapshot_fingerprint"] == observed.fingerprint
    failure_id = observed.failures[0].id
    assert current_ci["value"]["failures"] == {failure_id: dashboard["data"]["ci"]["failures"][failure_id]}


@pytest.mark.anyio
async def test_ci_rerun_requires_new_analysis(ci_plugin, ci_mocks, monkeypatch):
    plugin, pr = ci_plugin
    ci_mocks["read_review_dashboard"].return_value = existing_dashboard(snapshot())
    monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", Mock(return_value=snapshot(attempt=2)))

    prepared = await plugin.prepare_task(event_task(plugin, pr), (), None)

    assert prepared.action is TaskAction.RUN
    ci_mocks["apply_ci_patch"].assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["labels", "head", "retry", "closed", "refresh-error"])
async def test_ci_publication_rechecks_current_eligibility_and_failures(ci_plugin, ci_mocks, monkeypatch, change):
    plugin, pr = ci_plugin
    ci_mocks["read_review_dashboard"].return_value = existing_dashboard(snapshot())
    current = snapshot(failed=False)
    if change == "labels":
        ci_mocks["_has_skip_review_label"].side_effect = [False, True]
    next_snapshot = snapshot(NEXT_HEAD, failed=False) if change == "head" else snapshot(attempt=2)
    if change not in {"head", "retry"}:
        next_snapshot = current
    ci_mocks["fetch_ci_snapshot"].side_effect = [current, next_snapshot]
    if change == "head":
        monkeypatch.setattr(
            plugin_module, "_fetch_pr", Mock(side_effect=[pr, pr.model_copy(update={"head_sha": NEXT_HEAD})])
        )
    if change == "closed":
        monkeypatch.setattr(
            plugin_module, "_fetch_pr", Mock(side_effect=[pr, pr.model_copy(update={"state": "closed"})])
        )
    if change == "refresh-error":
        ci_mocks["fetch_ci_snapshot"].side_effect = [current, ValueError("CI refresh failed")]

    queued = event_task(plugin, pr)
    if change in {"closed", "refresh-error"}:
        with pytest.raises(ValueError, match="no longer eligible|CI refresh failed"):
            await plugin.prepare_task(queued, (), None)
    else:
        assert (await plugin.prepare_task(queued, (), None)).action is TaskAction.IGNORED
    (preview,) = ci_mocks["apply_ci_patch"].call_args_list
    assert preview.kwargs == {"preview": True}


@pytest.mark.anyio
@pytest.mark.parametrize("config_updates", [{"dry_run": True}, {"post_reviews": False}])
async def test_ci_recovery_respects_read_only_publication(ci_plugin, ci_mocks, monkeypatch, config_updates):
    plugin, pr = ci_plugin
    plugin.config = plugin.config.model_copy(update=config_updates)
    monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", Mock(return_value=snapshot(failed=False)))

    prepared = await plugin.prepare_task(event_task(plugin, pr), (), None)

    assert prepared.action is TaskAction.IGNORED
    ci_mocks["read_review_dashboard"].assert_not_called()
    ci_mocks["apply_ci_patch"].assert_not_called()


@pytest.mark.anyio
async def test_push_joins_older_queued_ci_task_and_keeps_the_normal_review_path(ci_plugin, monkeypatch):
    plugin, pr = ci_plugin
    ci = event_task(plugin, pr)
    code = event_task(plugin, pr, "pull_request_synchronize", "code-1")
    store = plugin.state_store
    assert store.record_task(ci)
    created_at = store.task_run(ci.task_id).created_at
    assert created_at is not None
    assert store.enqueue_task(code, coalesce_since=created_at + 1) == (True, ci.task_id)
    (coalesced,) = store.coalesced_tasks_for(ci.task_id)
    inventory = {
        "head_sha": HEAD,
        "base_ref": "main",
        "merge_base_sha": NEXT_HEAD,
        "inventory_id": "c" * 64,
        "files": [],
    }
    build = Mock(return_value=inventory)
    monkeypatch.setattr(plugin_module, "build_inventory", build)

    prepared = await plugin.prepare_task(ci, (AgentTask.model_validate(coalesced.task),), None)

    assert [trigger["kind"] for trigger in prepared.metadata["triggers"]] == [
        "ci_changed",
        "pull_request_synchronize",
    ]
    assert prepared.metadata["review_inventory"] == inventory
    assert "ci_snapshot" not in prepared.metadata
    build.assert_called_once()


@pytest.mark.anyio
async def test_ci_child_pins_failed_executions_and_uses_configured_fast_model_without_scope(ci_plugin):
    plugin, pr = ci_plugin
    parent = event_task(plugin, pr)
    request = SubtaskRequest(
        request_key="ci-analysis",
        purpose="ci-analysis",
        prompt="Explain whether the pinned Python 3.12 failure comes from startup or the test body",
        inputs={"fingerprint": snapshot().fingerprint},
        execution=ExecutionOverride(model="parent-selected-model"),
    )

    prepared = await plugin.prepare_subtask(parent, request)
    execution = plugin.runtime.config.resolve_execution(prepared.kind, prepared.execution)

    assert prepared.kind == "github_reviewer.ci-analysis"
    assert prepared.revision == HEAD
    assert prepared.inputs["ci_snapshot"]["fingerprint"] == snapshot().fingerprint
    assert prepared.inputs["ci_snapshot"]["failures"][0]["run_attempt"] == 1
    assert not prepared.memory_enabled
    assert execution.backend == "claude"
    assert execution.model == "deepseek-v4.1-flash-ali"
    assert execution.reasoning == "medium"
    assert execution.turn_timeout_seconds == 600
    assert "review_scope" not in parent.metadata
    assert request.prompt in prepared.prompt
    assert request.prompt not in prepared.developer_instructions


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["rerun", "recovered", "head", "nested"])
async def test_ci_child_rejects_outdated_or_unowned_analysis(ci_plugin, monkeypatch, change):
    plugin, pr = ci_plugin
    parent = event_task(plugin, pr)
    if change == "rerun":
        monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", lambda *args: snapshot(attempt=2))
    elif change == "recovered":
        monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", lambda *args: snapshot(failed=False))
    elif change == "head":
        monkeypatch.setattr(plugin_module, "_fetch_pr", lambda *args: pr.model_copy(update={"head_sha": NEXT_HEAD}))
        monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", lambda *args: snapshot(NEXT_HEAD))
    else:
        parent = parent.model_copy(update={"spawned_by_task_id": "another-root"})
    request = SubtaskRequest(
        request_key="ci-analysis",
        purpose="ci-analysis",
        prompt="Analyze failures",
        inputs={"fingerprint": snapshot().fingerprint},
    )

    with pytest.raises(ValueError, match="CI changed|no current failures|head changed|root reviewer"):
        await plugin.prepare_subtask(parent, request)


@pytest.mark.anyio
async def test_ci_refresh_rechecks_recovery_without_mutating_review_state(ci_plugin, monkeypatch):
    plugin, pr = ci_plugin
    parent = event_task(plugin, pr)
    before = parent.model_dump()
    refresh = next(tool for tool in plugin.task_control_tools() if tool.name == "ci-refresh")
    failed = await refresh.invoke(parent, {})
    monkeypatch.setattr(plugin_module, "fetch_ci_snapshot", lambda *args: snapshot(failed=False))

    recovered = await refresh.invoke(parent, {})

    assert failed["snapshot"]["failures"]
    assert recovered["snapshot"]["failures"] == []
    assert recovered["snapshot"]["fingerprint"] != failed["snapshot"]["fingerprint"]
    assert parent.model_dump() == before
    with pytest.raises(ValueError, match="not available"):
        await refresh.invoke(parent.model_copy(update={"spawned_by_task_id": "root"}), {})


@pytest.mark.anyio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_ci_children_survive_recovery_without_a_changed_file_assignment(ci_plugin, corrupt):
    plugin, pr = ci_plugin
    parent = event_task(plugin, pr)
    store = plugin.state_store
    store.record_task(parent)
    store.mark_task_running(parent.task_id, None)
    request = SubtaskRequest(
        request_key="ci-analysis",
        purpose="ci-analysis",
        prompt="Analyze failures",
        inputs={"fingerprint": snapshot().fingerprint},
    )
    prepared = await plugin.prepare_subtask(parent, request)
    child = store.create_subtask(
        parent.task_id,
        request,
        prepared=prepared,
        execution=plugin.runtime.config.resolve_execution(prepared.kind, prepared.execution),
    )
    if corrupt:
        store.update_task_input(
            child.model_copy(update={"workspace": child.workspace.model_copy(update={"revision": NEXT_HEAD})})
        )
    plugin.state_store = StateStore(store.db_path)

    plugin._validate_recovered_subtasks()

    assert plugin.state_store.task_status(child.task_id) == ("failed" if corrupt else "queued")
    assert "review_scope" not in plugin.state_store.task_request(child.task_id).metadata["inputs"]
