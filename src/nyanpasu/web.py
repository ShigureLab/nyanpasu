from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, FastAPI

from nyanpasu.agent import AgentService, PostProcessHook
from nyanpasu.config import NyanpasuConfig, ensure_state_dirs, load_config
from nyanpasu.plugins import PluginManager, PluginRegistry
from nyanpasu.store import StateStore

if TYPE_CHECKING:
    from enum import Enum

    from nyanpasu.models import AgentTask, TaskRunResult


class AgentBackend(Protocol):
    async def submit(self, task: AgentTask) -> dict[str, Any]: ...

    async def run_now(self, task: AgentTask) -> TaskRunResult: ...

    async def shutdown(self) -> None: ...

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None: ...


class WebPluginRuntime:
    def __init__(self, *, config: NyanpasuConfig, app: FastAPI | None, agent: AgentBackend) -> None:
        self.config = config
        self.app = app
        self.agent = agent

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        return await self.agent.submit(task)

    async def run_now(self, task: AgentTask) -> TaskRunResult:
        return await self.agent.run_now(task)

    def add_router(self, router: APIRouter, *, prefix: str = "", tags: list[str] | None = None) -> None:
        if self.app is None:
            raise RuntimeError("web plugin runtime is not bound to an app")
        tag_values: list[str | Enum] | None = list(tags) if tags is not None else None
        self.app.include_router(router, prefix=prefix, tags=tag_values)

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None:
        self.agent.add_post_process_hook(plugin_id, hook)


def create_app(
    config: NyanpasuConfig | None = None,
    agent: AgentBackend | None = None,
    *,
    plugin_registry: PluginRegistry | None = None,
) -> FastAPI:
    resolved_config = config or load_config()
    ensure_state_dirs(resolved_config)
    resolved_agent = agent or AgentService(resolved_config)
    runtime = WebPluginRuntime(config=resolved_config, app=None, agent=resolved_agent)
    plugin_manager = PluginManager(resolved_config, runtime, plugin_registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await plugin_manager.setup()
        try:
            yield
        finally:
            await plugin_manager.shutdown()
            await resolved_agent.shutdown()

    app = FastAPI(title="Nyanpasu Agent Service", version="0.1.0", lifespan=lifespan)
    app.state.config = resolved_config
    app.state.agent = resolved_agent
    runtime.app = app

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "backend": resolved_config.codex.backend,
            "enabled_plugins": list(resolved_config.enabled_plugins or resolved_config.plugins),
        }

    @app.get("/tasks")
    async def tasks(limit: int = 20) -> dict[str, Any]:
        store = StateStore(resolved_config.db_path)
        return {"tasks": [task.model_dump(mode="json") for task in store.recent_tasks(limit)]}

    @app.get("/contexts")
    async def contexts() -> dict[str, Any]:
        store = StateStore(resolved_config.db_path)
        return {
            "contexts": [
                {
                    "context_key": context.context_key,
                    "thread_id": context.thread_id,
                    "session_worktree": str(context.session_worktree) if context.session_worktree else None,
                    "workspace_key": context.workspace_key,
                    "revision": context.revision,
                }
                for context in store.list_contexts()
            ]
        }

    return app


def app_from_env() -> FastAPI:
    return create_app(load_config())


app = app_from_env()
