from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.models import AgentContext, AgentTask, TaskAction
from nyanpasu.plugins import PluginRegistry
from nyanpasu.store import StateStore
from nyanpasu.web import create_app

if TYPE_CHECKING:
    from nyanpasu.models import TaskRunResult


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
async def test_token_protects_all_data_routes_and_plugins(tmp_path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path, server=ServerConfig(token=SecretStr("test-secret")), plugins={"enabled": ["fake"]}
    )
    registry = PluginRegistry({"fake": FakePlugin()})
    app = create_app(config, agent=FakeAgent(), plugin_registry=registry)
    paths = [
        "/tasks",
        "/contexts",
        "/api/dashboard",
        "/api/overview",
        "/api/runtime",
        "/api/memory",
        "/api/memory?task_id=missing",
        "/api/memory/missing",
        "/api/plugins",
        "/api/tasks",
        "/api/tasks/missing",
        "/api/sessions",
        "/api/sessions/missing",
        "/api/sessions/missing/transcript",
        "/api/sessions/missing/entries/entry",
        "/api/sessions/missing/search?q=secret",
        "/api/sessions/missing/content/ref?download=true",
        "/api/sessions/missing/export?format=jsonl",
        "/plugins/fake/ping",
    ]
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            for path in paths:
                for authorization in [None, "Bearer wrong", "Basic test-secret", "Bearer"]:
                    response = await client.get(path, headers={"Authorization": authorization} if authorization else {})
                    assert response.status_code == 401, (path, authorization)
                    assert response.headers["WWW-Authenticate"] == "Bearer"
                    assert response.headers["Cache-Control"] == "no-store"
                response = await client.get(path, headers={"Authorization": "bearer test-secret"})
                assert response.status_code != 401, path
                assert "test-secret" not in response.text
                assert response.headers["Cache-Control"] == "no-store"
            # Tokens are accepted only in the Authorization header, never in URLs or cookies.
            assert (
                await client.get("/api/overview?token=test-secret", headers={"Cookie": "token=test-secret"})
            ).status_code == 401
            for path in ["/health", "/dashboard"]:
                assert (await client.get(path)).status_code == 200


@pytest.mark.anyio
async def test_dashboard_assets_remain_public_with_auth(tmp_path, monkeypatch) -> None:
    static = tmp_path / "assets"
    static.mkdir()
    (static / "index.html").write_text('<script src="/dashboard/assets/app.js"></script>')
    (static / "app.js").write_text("console.log('login shell')")
    monkeypatch.setattr("nyanpasu.web.DASHBOARD_STATIC_DIR", static)
    app = create_app(
        NyanpasuConfig(state_dir=tmp_path, server=ServerConfig(token=SecretStr("test-secret"))),
        agent=FakeAgent(),
        plugin_registry=PluginRegistry(),
    )
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/dashboard/assets/app.js")).status_code == 200


@pytest.mark.anyio
async def test_app_health_and_plugin_router(tmp_path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        plugins={"enabled": ["fake"], "settings": {"fake": {"enabled": True}}},
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
    store = StateStore(config.db_path)
    store.record_task(
        AgentTask(
            execution=config.resolve_execution(),
            task_id="task-1",
            context_key="demo:1",
            action=TaskAction.RUN,
            prompt="request",
        )
    )
    store.upsert_context(
        AgentContext(
            context_key="demo:1",
            thread_id="thread-1",
            session_worktree=tmp_path / "worktree",
            workspace_key="demo",
            revision="head-a",
        )
    )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        tasks = await client.get("/tasks")
        contexts = await client.get("/contexts")

    assert tasks.status_code == 200
    assert [(task["task_id"], task["context_key"], task["status"]) for task in tasks.json()["tasks"]] == [
        ("task-1", "demo:1", "queued")
    ]
    assert contexts.status_code == 200
    assert [
        (context["context_key"], context["thread_id"], context["session_worktree"])
        for context in contexts.json()["contexts"]
    ] == [("demo:1", "thread-1", str(tmp_path / "worktree"))]


@pytest.mark.anyio
async def test_runtime_exposes_configured_model_and_effort(tmp_path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path,
        backends={
            "codex": {"driver": "codex", "defaults": {"model": "configured-model", "reasoning": "medium"}},
            "cheap": {"driver": "claude-code", "defaults": {"model": "small-model", "reasoning": "low"}},
        },
        tasks={"kinds": {"memory_consolidation": {"execution": {"backend": "cheap"}}}},
    )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            runtime = await client.get("/api/runtime")

    assert runtime.status_code == 200
    data = runtime.json()
    assert "backend" not in data
    assert data["default_execution"]["model"] == "configured-model"
    assert data["default_execution"]["reasoning"] == "medium"
    assert data["task_kinds"]["memory_consolidation"]["backend"] == "cheap"
    assert data["task_kinds"]["memory_consolidation"]["model"] == "small-model"
    assert (
        data["task_kinds"]["memory_consolidation"]["sources"]["backend"] == "tasks.kinds.memory_consolidation.execution"
    )
    assert set(data["backends"]) == {"codex", "cheap"}
    assert data["backends"]["cheap"]["driver"] == "claude-code"
