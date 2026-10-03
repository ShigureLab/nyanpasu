from __future__ import annotations

import hashlib
import hmac
import importlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from test_github_events import issue_comment_payload, pr_payload, pull_request_review_payload, review_comment_payload

from nyanpasu.agent import AgentService
from nyanpasu.config import MemoryConfig, NyanpasuConfig, PluginsConfig, ServerConfig
from nyanpasu.plugins import PluginRegistry
from nyanpasu.store import StateStore
from nyanpasu.web import create_app
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import PullRequestRef
from nyanpasu_github_reviewer.plugin import GitHubPollAgent, GitHubReviewerPlugin, verify_signature
from nyanpasu_github_reviewer.poller import GitHubEventsPoller

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.models import AgentTask, TaskRunResult


class FakeAgent:
    def __init__(self) -> None:
        self.tasks: list[AgentTask] = []

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        self.tasks.append(task)
        return {"accepted": True, "task_id": task.task_id, "action": task.action.value}

    async def run_now(self, task: AgentTask) -> TaskRunResult:
        self.tasks.append(task)
        raise NotImplementedError

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def add_task_control_handler(self, plugin_id, handler) -> None:
        pass

    def add_subtask_preparer(self, plugin_id, preparer) -> None:
        pass

    def add_task_preparer(self, plugin_id, preparer) -> None:
        self.preparer = preparer

    def add_post_process_hook(self, plugin_id, hook) -> None:
        _ = plugin_id, hook


def test_verify_signature_rejects_invalid_signature() -> None:
    with pytest.raises(HTTPException):
        verify_signature(b"{}", "sha256=bad", "secret")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "server_token,webhook_secret", [(None, None), ("dashboard-token", None), ("dashboard-token", "webhook-secret")]
)
async def test_webhook_accepts_event(tmp_path: Path, server_token, webhook_secret) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        memory=MemoryConfig(enabled=False),
        server=ServerConfig(token=SecretStr(server_token) if server_token else None),
        integrations={"github": {"token": "webhook-token"}},
        plugins=PluginsConfig(
            enabled=("github_reviewer",),
            settings={
                "github_reviewer": {
                    "poll_enabled": False,
                    "dry_run": True,
                    "post_reviews": False,
                    "webhook_secret": webhook_secret,
                    "repos": {"ExampleOrg/ExampleRepo": {"local_path": str(tmp_path / "repo")}},
                }
            },
        ),
    )
    fake_agent = FakeAgent()
    registry = PluginRegistry()
    registry.register(GitHubReviewerPlugin())
    app = create_app(config, agent=fake_agent, plugin_registry=registry)
    body = json.dumps(pr_payload()).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "delivery-1"}
    if webhook_secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    elif server_token:
        headers["Authorization"] = f"Bearer {server_token}"
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            if server_token:
                rejected = await client.post("/plugins/github-reviewer/webhook", content=body)
                assert rejected.status_code == 401
                assert fake_agent.tasks == []
            if webhook_secret:
                rejected = await client.post(
                    "/plugins/github-reviewer/webhook",
                    content=body,
                    headers={"Authorization": f"Bearer {server_token}"},
                )
                assert rejected.status_code == 401
            response = await client.post(
                "/plugins/github-reviewer/webhook",
                content=body,
                headers=headers,
            )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert fake_agent.tasks[0].task_id == "delivery-1"
    assert fake_agent.tasks[0].context_key == "github:ExampleOrg/ExampleRepo#123"
    assert fake_agent.tasks[0].metadata["triggers"][0]["kind"] == "pull_request_synchronize"


@pytest.mark.anyio
@pytest.mark.parametrize("poll_first", [False, True])
@pytest.mark.parametrize(
    "github_event,event_type,payload",
    [
        ("pull_request", "PullRequestEvent", pr_payload("opened")),
        ("pull_request", "PullRequestEvent", pr_payload("synchronize")),
        ("issue_comment", "IssueCommentEvent", issue_comment_payload("/review")),
        ("pull_request_review_comment", "PullRequestReviewCommentEvent", review_comment_payload(body="/review")),
        ("pull_request_review", "PullRequestReviewEvent", pull_request_review_payload("/review")),
    ],
)
async def test_webhook_and_polling_enqueue_same_event_once(
    tmp_path: Path, monkeypatch, poll_first: bool, github_event: str, event_type: str, payload: dict[str, Any]
) -> None:
    repo = "ExampleOrg/ExampleRepo"
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        memory=MemoryConfig(enabled=False),
        plugins=PluginsConfig(
            enabled=("github_reviewer",),
            settings={
                "github_reviewer": {"poll_enabled": False, "repos": {repo: {"local_path": str(tmp_path / "repo")}}}
            },
        ),
    )
    agent = AgentService(config)
    # Exercise real admission and persistence without launching a reviewer.
    monkeypatch.setattr(agent, "_schedule", lambda _: None)
    monkeypatch.setattr(
        importlib.import_module("nyanpasu_github_reviewer.plugin"),
        "_fetch_pr",
        lambda *_: PullRequestRef.from_github(repo, pr_payload()["pull_request"]),
    )
    plugin = GitHubReviewerPlugin()
    registry = PluginRegistry()
    registry.register(plugin)
    app = create_app(config, agent=agent, plugin_registry=registry)
    async with app.router.lifespan_context(app):
        assert plugin.store is not None
        assert plugin.config is not None
        plugin.store.upsert_poll_event_cursor(repo, last_event_created_at="2026-05-30T09:59:00Z", cursor_event_ids=())
        poller = GitHubEventsPoller(
            plugin.config,
            store=plugin.store,
            agent=GitHubPollAgent(plugin),
            list_repo_events=lambda *_: [
                {
                    "id": "1",
                    "type": event_type,
                    "repo": {"name": repo},
                    "created_at": "2026-05-30T10:00:00Z",
                    "payload": payload,
                }
            ],
            list_pull_requests=lambda *_: [],
            list_pull_request_timeline=lambda *_: [],
        )
        if poll_first:
            polled = await poller.run_once()
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/plugins/github-reviewer/webhook",
                json=payload,
                headers={"X-GitHub-Event": github_event, "X-GitHub-Delivery": "webhook-delivery"},
            )
        if not poll_first:
            polled = await poller.run_once()
        assert response.status_code == 202
        assert response.json()["accepted"] is (not poll_first)
        assert polled.submitted == int(poll_first)
        assert polled.duplicates == int(not poll_first)
        tasks = agent.store.unfinished_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_id == ("events-poll-ExampleOrg-ExampleRepo-1" if poll_first else "webhook-delivery")
        with plugin.store._connect() as conn:
            journal = conn.execute("SELECT dedupe_key, status FROM github_event_journal").fetchall()
        assert len(journal) == 1
        assert journal[0]["dedupe_key"] == tasks[0].dedupe_key
        assert journal[0]["status"] == ("completed" if poll_first else "skipped")
    reopened = StateStore(config.db_path)
    redelivered = parse_github_event(github_event, "delivery-after-restart", payload)
    task = plugin.event_to_task(redelivered)
    assert not reopened.record_task(task.model_copy(update={"execution": config.resolve_execution(task.kind)}))
