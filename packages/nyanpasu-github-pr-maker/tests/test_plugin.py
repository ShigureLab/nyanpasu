from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
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


@pytest.mark.anyio
async def test_pr_maker_accepts_task_and_registers_post_process(tmp_path: Path, monkeypatch) -> None:
    agentic_module = importlib.import_module("nyanpasu_github.agentic")
    monkeypatch.setattr(agentic_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        enabled_plugins=("github_pr_maker",),
        integrations={"github": {"token_env": "NYANPASU_TEST_GH_TOKEN"}},
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
    registry.register(GitHubPrMakerPlugin())
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
    assert "gh_env" not in task.metadata["publish"]
    assert "token" not in str(task.metadata)
    assert "Create a branch, commit your changes, push the branch, and open exactly one pull request" in task.prompt
    assert "gh pr create" in task.prompt
    assert "NO_PR: <reason>" in task.prompt
    assert "Dry run:" in task.prompt
    assert "Do not commit, push, or create a pull request." in task.prompt
    assert "PR: <url>" in task.prompt
    assert "NYANPASU_TEST_GH_TOKEN" in task.prompt


@pytest.mark.anyio
async def test_pr_maker_post_process_records_agent_created_pr(tmp_path: Path, monkeypatch) -> None:
    agentic_module = importlib.import_module("nyanpasu_github.agentic")
    monkeypatch.setattr(agentic_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    plugin = GitHubPrMakerPlugin()
    runtime = FakeRuntime(
        tmp_path,
        integrations={
            "github": {
                "token": "token-1",
                "git_author_name": "Shared Bot",
                "git_author_email": "shared@example.com",
            }
        },
    )
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
            final_message="Implemented docs.\nPR: https://github.com/ExampleOrg/ExampleRepo/pull/1",
            raw_events=[],
            session_worktree=tmp_path / "worktree",
        ),
    )

    assert plugin.store is not None
    record = plugin.store.get("task-2")
    assert record is not None
    assert record.status == "published"
    assert record.pr_url == "https://github.com/ExampleOrg/ExampleRepo/pull/1"
    assert record.pr_number == 1
    assert record.result["agentic"] is True
    assert plugin.store.list_active_managed_prs() == ()
    assert task.metadata["publish"]["git_author_name"] == "Shared Bot"
    assert "token-1" not in str(task.metadata)


@pytest.mark.anyio
async def test_pr_maker_records_failure_when_agent_omits_pr_url(tmp_path: Path, monkeypatch) -> None:
    agentic_module = importlib.import_module("nyanpasu_github.agentic")
    monkeypatch.setattr(agentic_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    plugin = GitHubPrMakerPlugin()
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
    task = plugin.request_to_task(
        CreatePullRequestTaskRequest(repo="ExampleOrg/ExampleRepo", task="Update docs.", task_id="task-3")
    )

    await plugin._post_process(
        task,
        TaskRunResult(
            task_id="task-3",
            status=TaskStatus.COMPLETED,
            thread_id="thread-1",
            turn_id="turn-1",
            final_message="Implemented docs.",
            raw_events=[],
            session_worktree=tmp_path / "worktree",
        ),
    )

    assert plugin.store is not None
    record = plugin.store.get("task-3")
    assert record is not None
    assert record.status == "failed"
    assert record.error == "agent final message did not include `PR: <url>`"


@pytest.mark.anyio
async def test_pr_maker_dry_run_records_without_pr_url(tmp_path: Path, monkeypatch) -> None:
    agentic_module = importlib.import_module("nyanpasu_github.agentic")
    monkeypatch.setattr(agentic_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    plugin = GitHubPrMakerPlugin()
    runtime = FakeRuntime(tmp_path)
    await plugin.setup(
        runtime,
        {
            "dry_run": True,
            "follow_up_enabled": False,
            "repos": {
                "ExampleOrg/ExampleRepo": {
                    "local_path": str(tmp_path / "repo"),
                    "github_remote": "git@github.com:ExampleOrg/ExampleRepo.git",
                }
            },
        },
    )
    task = plugin.request_to_task(
        CreatePullRequestTaskRequest(repo="ExampleOrg/ExampleRepo", task="Update docs.", task_id="task-4")
    )

    await plugin._post_process(
        task,
        TaskRunResult(
            task_id="task-4",
            status=TaskStatus.COMPLETED,
            thread_id="thread-1",
            turn_id="turn-1",
            final_message="Dry run complete.",
            raw_events=[],
            session_worktree=tmp_path / "worktree",
        ),
    )

    assert plugin.store is not None
    record = plugin.store.get("task-4")
    assert record is not None
    assert record.status == "dry_run"
    assert record.pr_url is None
    assert record.error is None


@pytest.mark.anyio
async def test_pr_maker_records_agent_no_pr_reason(tmp_path: Path, monkeypatch) -> None:
    agentic_module = importlib.import_module("nyanpasu_github.agentic")
    monkeypatch.setattr(agentic_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    plugin = GitHubPrMakerPlugin()
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
    task = plugin.request_to_task(
        CreatePullRequestTaskRequest(repo="ExampleOrg/ExampleRepo", task="Update docs.", task_id="task-5")
    )

    await plugin._post_process(
        task,
        TaskRunResult(
            task_id="task-5",
            status=TaskStatus.COMPLETED,
            thread_id="thread-1",
            turn_id="turn-1",
            final_message="Nothing to change.\nNO_PR: repository already contains the requested docs",
            raw_events=[],
            session_worktree=tmp_path / "worktree",
        ),
    )

    assert plugin.store is not None
    record = plugin.store.get("task-5")
    assert record is not None
    assert record.status == "no_changes"
    assert record.pr_url is None
    assert record.error is None
    assert record.result["no_pr_reason"] == "repository already contains the requested docs"


@pytest.mark.anyio
async def test_pr_maker_registers_managed_pr_when_follow_up_enabled(tmp_path: Path, monkeypatch) -> None:
    agentic_module = importlib.import_module("nyanpasu_github.agentic")
    plugin_module = importlib.import_module("nyanpasu_github_pr_maker.plugin")
    monkeypatch.setattr(agentic_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    monkeypatch.setattr(plugin_module, "fetch_pull_request_view", lambda *_, **__: _pr_view(head_sha="abc123"))
    plugin = GitHubPrMakerPlugin()
    runtime = FakeRuntime(tmp_path, integrations={"github": {"token": "token"}})
    await plugin.setup(
        runtime,
        {
            "follow_up_enabled": True,
            "follow_up_interval_seconds": 3600,
            "git_author_name": "Plugin Bot",
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
                task_id="task-6",
                branch_name="nyanpasu/task-6",
            )
        )

        await plugin._post_process(
            task,
            TaskRunResult(
                task_id="task-6",
                status=TaskStatus.COMPLETED,
                thread_id="thread-1",
                turn_id="turn-1",
                final_message="Done.\nPR: https://github.com/ExampleOrg/ExampleRepo/pull/1",
                raw_events=[],
                session_worktree=tmp_path / "worktree",
            ),
        )
    finally:
        await plugin.shutdown()

    assert plugin.store is not None
    managed = plugin.store.list_active_managed_prs()
    assert len(managed) == 1
    assert managed[0].context_key == "github-pr-maker:ExampleOrg/ExampleRepo:task-6"
    assert managed[0].pr_number == 1
    assert managed[0].git_author_name == "Plugin Bot"


@pytest.mark.anyio
async def test_pr_maker_follow_up_poller_dispatches_changed_pr(tmp_path: Path, monkeypatch) -> None:
    followup_module = importlib.import_module("nyanpasu_github_pr_maker.followup")
    first = _pr_view(head_sha="abc123")
    second = _pr_view(head_sha="def456", failing_checks=("unit",))
    seen_env: list[dict[str, str] | None] = []

    def fetch_pr(*args, **kwargs):
        _ = args
        seen_env.append(kwargs.get("env"))
        return second

    monkeypatch.setattr(followup_module, "fetch_pull_request_view", fetch_pr)
    plugin = GitHubPrMakerPlugin()
    runtime = FakeRuntime(tmp_path, integrations={"github": {"token_env": "NYANPASU_TEST_GH_TOKEN"}})
    monkeypatch.setenv("NYANPASU_TEST_GH_TOKEN", "token")
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
        git_author_name="Bot",
        git_author_email="bot@example.com",
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
        assert follow_up_task.metadata["publish"]["git_author_name"] == "Bot"
        assert seen_env == [{"GH_TOKEN": "token", "GITHUB_TOKEN": "token"}]
        assert "failing checks: unit" in follow_up_task.prompt
        assert "push to the existing PR branch" in follow_up_task.prompt
        assert "open another pull request" in follow_up_task.prompt
        assert "NYANPASU_TEST_GH_TOKEN" in follow_up_task.prompt
        assert "token" not in str(follow_up_task.metadata)
    finally:
        await plugin.shutdown()


class FakeRuntime:
    def __init__(self, tmp_path: Path, *, integrations: dict[str, dict[str, Any]] | None = None) -> None:
        self.config = NyanpasuConfig(state_dir=tmp_path / "state", integrations=integrations or {})
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
