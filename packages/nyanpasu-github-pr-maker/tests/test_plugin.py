from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from nyanpasu_github.publisher import PublishPullRequestRequest, PublishPullRequestResult
from nyanpasu_github.pulls import PullRequestView
from nyanpasu_github_pr_maker.followup import GitHubPrMakerFollowUpPoller
from nyanpasu_github_pr_maker.models import CreatePullRequestTaskRequest
from nyanpasu_github_pr_maker.plugin import GitHubPrMakerPlugin

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import TaskRunResult, TaskStatus
from nyanpasu.plugins import PluginRegistry
from nyanpasu.web import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.models import AgentTask


class FakeAgent:
    def __init__(self) -> None:
        self.tasks: list[AgentTask] = []
        self.hooks: dict[str, Any] = {}

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        self.tasks.append(task)
        return {"accepted": True, "task_id": task.task_id, "action": task.action.value}

    async def run_now(self, task: AgentTask) -> TaskRunResult:
        self.tasks.append(task)
        raise NotImplementedError

    async def shutdown(self) -> None:
        return None

    def add_post_process_hook(self, plugin_id, hook) -> None:
        self.hooks[plugin_id] = hook


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, PublishPullRequestRequest]] = []

    def publish(self, *, worktree: Path, request: PublishPullRequestRequest) -> PublishPullRequestResult:
        self.calls.append((worktree, request))
        return PublishPullRequestResult(
            status="published",
            branch_name=request.branch_name,
            changed_files=("README.md",),
            commit_sha="abc123",
            pr_url="https://github.com/ExampleOrg/ExampleRepo/pull/1",
            pr_number=1,
        )


@pytest.mark.anyio
async def test_pr_maker_accepts_task_and_registers_post_process(tmp_path: Path, monkeypatch) -> None:
    plugin_module = importlib.import_module("nyanpasu_github_pr_maker.plugin")
    monkeypatch.setattr(plugin_module, "resolve_branch_sha", lambda *_: "base-sha")
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        enabled_plugins=("github_pr_maker",),
        plugins={
            "github_pr_maker": {
                "default_base_branch": "develop",
                "dry_run": True,
                "follow_up_enabled": False,
                "repos": {
                    "ExampleOrg/ExampleRepo": {
                        "local_path": str(tmp_path / "repo"),
                        "github_remote": "git@github.com:ExampleOrg/ExampleRepo.git",
                        "base_branches": ["develop"],
                    }
                },
            }
        },
    )
    fake_agent = FakeAgent()
    registry = PluginRegistry()
    registry.register(GitHubPrMakerPlugin(publisher=FakePublisher()))
    app = create_app(config, agent=fake_agent, plugin_registry=registry)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/plugins/github-pr-maker/tasks",
                json={
                    "repo": "ExampleOrg/ExampleRepo",
                    "task": "Update the README with setup instructions.",
                    "title": "Update README setup docs",
                    "task_id": "task-1",
                },
            )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert "github_pr_maker" in fake_agent.hooks
    task = fake_agent.tasks[0]
    assert task.task_id == "task-1"
    assert task.context_key == "github-pr-maker:ExampleOrg/ExampleRepo:task-1"
    assert task.workspace is not None
    assert task.workspace.ref == "refs/heads/develop"
    assert task.workspace.revision == "base-sha"
    assert task.metadata["publish"]["dry_run"] is True
    assert "Do not push" in task.prompt


@pytest.mark.anyio
async def test_pr_maker_post_process_publishes_and_records_result(tmp_path: Path, monkeypatch) -> None:
    plugin_module = importlib.import_module("nyanpasu_github_pr_maker.plugin")
    monkeypatch.setattr(plugin_module, "resolve_branch_sha", lambda *_: "base-sha")
    monkeypatch.setattr(plugin_module, "fetch_pull_request_view", lambda *_: _pr_view(head_sha="abc123"))
    publisher = FakePublisher()
    plugin = GitHubPrMakerPlugin(publisher=publisher)
    config = {
        "follow_up_enabled": False,
        "repos": {
            "ExampleOrg/ExampleRepo": {
                "local_path": str(tmp_path / "repo"),
                "github_remote": "git@github.com:ExampleOrg/ExampleRepo.git",
            }
        },
    }
    runtime = FakeRuntime(tmp_path)
    await plugin.setup(runtime, config)
    task = plugin.request_to_task(
        CreatePullRequestTaskRequest(
            repo="ExampleOrg/ExampleRepo",
            task="Update docs.",
            task_id="task-2",
            branch_name="nyanpasu/task-2",
        )
    )

    await plugin._post_process(
        task,
        TaskRunResult(
            task_id="task-2",
            status=TaskStatus.COMPLETED,
            thread_id="thread-1",
            turn_id="turn-1",
            final_message="done",
            raw_events=[],
            session_worktree=tmp_path / "worktree",
        ),
    )

    assert publisher.calls[0][0] == tmp_path / "worktree"
    assert plugin.store is not None
    record = plugin.store.get("task-2")
    assert record is not None
    assert record.status == "published"
    assert record.pr_url == "https://github.com/ExampleOrg/ExampleRepo/pull/1"
    assert record.pr_number == 1
    assert plugin.store.list_active_managed_prs() == ()


@pytest.mark.anyio
async def test_pr_maker_registers_managed_pr_when_follow_up_enabled(tmp_path: Path, monkeypatch) -> None:
    plugin_module = importlib.import_module("nyanpasu_github_pr_maker.plugin")
    monkeypatch.setattr(plugin_module, "resolve_branch_sha", lambda *_: "base-sha")
    monkeypatch.setattr(plugin_module, "fetch_pull_request_view", lambda *_: _pr_view(head_sha="abc123"))
    publisher = FakePublisher()
    plugin = GitHubPrMakerPlugin(publisher=publisher)
    runtime = FakeRuntime(tmp_path)
    await plugin.setup(
        runtime,
        {
            "follow_up_enabled": True,
            "follow_up_interval_seconds": 3600,
            "repos": {
                "ExampleOrg/ExampleRepo": {
                    "local_path": str(tmp_path / "repo"),
                    "github_remote": "git@github.com:ExampleOrg/ExampleRepo.git",
                }
            },
        },
    )
    try:
        task = plugin.request_to_task(
            CreatePullRequestTaskRequest(
                repo="ExampleOrg/ExampleRepo",
                task="Update docs.",
                task_id="task-3",
                branch_name="nyanpasu/task-3",
            )
        )

        await plugin._post_process(
            task,
            TaskRunResult(
                task_id="task-3",
                status=TaskStatus.COMPLETED,
                thread_id="thread-1",
                turn_id="turn-1",
                final_message="done",
                raw_events=[],
                session_worktree=tmp_path / "worktree",
            ),
        )
    finally:
        await plugin.shutdown()

    assert plugin.store is not None
    managed = plugin.store.list_active_managed_prs()
    assert len(managed) == 1
    assert managed[0].context_key == "github-pr-maker:ExampleOrg/ExampleRepo:task-3"
    assert managed[0].pr_number == 1


@pytest.mark.anyio
async def test_pr_maker_follow_up_poller_dispatches_changed_pr(tmp_path: Path, monkeypatch) -> None:
    followup_module = importlib.import_module("nyanpasu_github_pr_maker.followup")
    first = _pr_view(head_sha="abc123")
    second = _pr_view(head_sha="def456", failing_checks=("unit",))
    monkeypatch.setattr(followup_module, "fetch_pull_request_view", lambda *_: second)
    plugin = GitHubPrMakerPlugin(publisher=FakePublisher())
    runtime = FakeRuntime(tmp_path)
    await plugin.setup(
        runtime,
        {
            "follow_up_enabled": False,
            "repos": {
                "ExampleOrg/ExampleRepo": {
                    "local_path": str(tmp_path / "repo"),
                    "github_remote": "git@github.com:ExampleOrg/ExampleRepo.git",
                }
            },
        },
    )
    assert plugin.store is not None
    assert plugin.config is not None
    plugin.store.upsert_managed_pr(
        task_id="managed-pr-exampleorg-examplerepo-1",
        context_key="github-pr-maker:ExampleOrg/ExampleRepo:task-2",
        repo="ExampleOrg/ExampleRepo",
        pr_number=1,
        pr_url="https://github.com/ExampleOrg/ExampleRepo/pull/1",
        base_branch="main",
        branch_name="nyanpasu/task-2",
        title="Update docs",
        body="Body",
        task="Update docs.",
        last_digest=first.follow_up_digest(),
        last_head_sha=first.head_sha,
    )
    poller = GitHubPrMakerFollowUpPoller(plugin.config, store=plugin.store, runtime=runtime)

    try:
        submitted = await poller.run_once()

        assert submitted == 1
        follow_up_task = runtime.tasks[0]
        assert follow_up_task.context_key == "github-pr-maker:ExampleOrg/ExampleRepo:task-2"
        assert follow_up_task.workspace is not None
        assert follow_up_task.workspace.ref == "refs/heads/nyanpasu/task-2"
        assert follow_up_task.workspace.revision == "def456"
        assert follow_up_task.metadata["publish"]["existing_pr_number"] == 1
        assert "failing checks: unit" in follow_up_task.prompt
    finally:
        await plugin.shutdown()


class FakeRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.config = NyanpasuConfig(state_dir=tmp_path / "state")
        self.hooks: dict[str, Any] = {}
        self.tasks: list[AgentTask] = []

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        self.tasks.append(task)
        return {"accepted": True, "task_id": task.task_id}

    async def run_now(self, task: AgentTask):
        raise NotImplementedError

    def add_router(self, router, *, prefix: str = "", tags=None) -> None:
        _ = router, prefix, tags

    def add_post_process_hook(self, plugin_id: str, hook) -> None:
        self.hooks[plugin_id] = hook


def _pr_view(*, head_sha: str, failing_checks: tuple[str, ...] = ()) -> PullRequestView:
    return PullRequestView(
        repo="ExampleOrg/ExampleRepo",
        number=1,
        url="https://github.com/ExampleOrg/ExampleRepo/pull/1",
        state="open",
        draft=False,
        base_ref="main",
        head_ref="nyanpasu/task-2",
        head_sha=head_sha,
        review_decision="",
        merge_state_status="",
        updated_at="2026-05-31T00:00:00Z",
        failing_checks=failing_checks,
    )
