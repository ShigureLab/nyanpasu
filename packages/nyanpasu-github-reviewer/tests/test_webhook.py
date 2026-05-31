from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from test_github_events import pr_payload

from nyanpasu.config import NyanpasuConfig
from nyanpasu.plugins import PluginRegistry
from nyanpasu.web import create_app
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin, verify_signature

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

    async def shutdown(self) -> None:
        return None

    def add_post_process_hook(self, plugin_id, hook) -> None:
        _ = plugin_id, hook


def test_verify_signature_accepts_valid_signature() -> None:
    body = b'{"ok": true}'
    secret = "secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    verify_signature(body, f"sha256={digest}", secret)


def test_verify_signature_rejects_invalid_signature() -> None:
    with pytest.raises(HTTPException):
        verify_signature(b"{}", "sha256=bad", "secret")


@pytest.mark.anyio
async def test_webhook_accepts_event(tmp_path: Path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        enabled_plugins=("github_reviewer",),
        integrations={"github": {"token": "webhook-token"}},
        plugins={
            "github_reviewer": {
                "poll_enabled": False,
                "dry_run": True,
                "post_reviews": False,
                "repos": {"ExampleOrg/ExampleRepo": {"local_path": str(tmp_path / "repo")}},
            }
        },
    )
    fake_agent = FakeAgent()
    registry = PluginRegistry()
    registry.register(GitHubReviewerPlugin())
    app = create_app(config, agent=fake_agent, plugin_registry=registry)
    body = json.dumps(pr_payload()).encode()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                "/plugins/github-reviewer/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-1",
                },
            )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert fake_agent.tasks[0].task_id == "delivery-1"
    assert fake_agent.tasks[0].context_key == "github:ExampleOrg/ExampleRepo#123"
    assert fake_agent.tasks[0].metadata["raw"]["action"] == "synchronize"
