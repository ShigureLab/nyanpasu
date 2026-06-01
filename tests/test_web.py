from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction, TaskRunResult
from nyanpasu.plugins import PluginRegistry
from nyanpasu.store import StateStore
from nyanpasu.web import create_app


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


class FakePlugin:
    id = "fake"
    config_model = None

    async def setup(self, runtime, config) -> None:
        _ = config
        router = APIRouter()

        @router.get("/ping")
        async def ping() -> dict[str, str]:
            return {"pong": "yes", "state_dir": str(runtime.config.state_dir)}

        runtime.add_router(router, prefix="/plugins/fake", tags=["fake"])

    async def shutdown(self) -> None:
        return None


@pytest.mark.anyio
async def test_app_health_and_plugin_router(tmp_path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        enabled_plugins=("fake",),
        plugins={"fake": {"enabled": True}},
    )
    registry = PluginRegistry()
    registry.register(FakePlugin())
    app = create_app(config, agent=FakeAgent(), plugin_registry=registry)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            health = await client.get("/health")
            ping = await client.get("/plugins/fake/ping")

    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["enabled_plugins"] == ["fake"]
    assert ping.status_code == 200
    assert ping.json()["pong"] == "yes"


@pytest.mark.anyio
async def test_app_tasks_and_contexts_endpoints(tmp_path) -> None:
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        tasks = await client.get("/tasks")
        contexts = await client.get("/contexts")

    assert tasks.status_code == 200
    assert tasks.json() == {"tasks": []}
    assert contexts.status_code == 200
    assert contexts.json() == {"contexts": []}


@pytest.mark.anyio
async def test_app_dashboard_endpoints(tmp_path) -> None:
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    store = StateStore(config.db_path)
    assert store.record_task(
        AgentTask(
            task_id="task-1",
            action=TaskAction.RUN,
            context_key="demo:1",
            prompt="do work",
            dedupe_key="task-1",
            metadata={"plugin_id": "demo", "request": {"title": "Demo task", "repo": "owner/repo"}},
        )
    )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        api = await client.get("/api/dashboard")
        page = await client.get("/dashboard")

    assert api.status_code == 200
    data = api.json()
    assert data["totals"]["total"] == 1
    assert data["totals"]["queued"] == 1
    assert data["plugins"][0]["plugin_id"] == "demo"
    assert data["backlog"][0]["title"] == "Demo task"
    assert page.status_code == 200
    assert "Nyanpasu Dashboard" in page.text
    assert "/dashboard/assets/index-" in page.text
