from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from nyanpasu.agent import AgentService, PostProcessHook
from nyanpasu.auth import require_server_token
from nyanpasu.backends import Backends
from nyanpasu.config import NyanpasuConfig, ensure_state_dirs, load_config
from nyanpasu.plugins import PluginManager, PluginRegistry, SubtaskPreparer, TaskPreparer
from nyanpasu.store import StateStore
from nyanpasu.transcript.api import dashboard_router
from nyanpasu.transcript.queries import CursorError, TranscriptReader
from nyanpasu.transcript.source import RecordNotFound, SourceUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable
    from enum import Enum

    from nyanpasu.models import AgentTask, TaskRunResult
    from nyanpasu.transcript.history import SessionSource


class AgentBackend(Protocol):
    async def startup(self) -> None: ...

    async def submit(self, task: AgentTask) -> dict[str, Any]: ...

    async def run_now(self, task: AgentTask) -> TaskRunResult: ...

    async def shutdown(self) -> None: ...

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None: ...

    def add_task_preparer(self, plugin_id: str, preparer: TaskPreparer) -> None: ...

    def add_subtask_preparer(self, plugin_id: str, preparer: SubtaskPreparer) -> None: ...


class WebPluginRuntime:
    def __init__(self, *, config: NyanpasuConfig, app: FastAPI | None, agent: AgentBackend) -> None:
        self.config = config
        self.app = app
        self.agent = agent

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        return await self.agent.submit(task)

    async def run_now(self, task: AgentTask) -> TaskRunResult:
        return await self.agent.run_now(task)

    def add_router(
        self, router: APIRouter, *, prefix: str = "", tags: list[str] | None = None, require_auth: bool = True
    ) -> None:
        if self.app is None:
            raise RuntimeError("web plugin runtime is not bound to an app")
        tag_values: list[str | Enum] | None = list(tags) if tags is not None else None
        self.app.include_router(
            router, prefix=prefix, tags=tag_values, dependencies=[Depends(require_server_token)] if require_auth else []
        )

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None:
        self.agent.add_post_process_hook(plugin_id, hook)

    def add_task_preparer(self, plugin_id: str, preparer: TaskPreparer) -> None:
        self.agent.add_task_preparer(plugin_id, preparer)

    def add_subtask_preparer(self, plugin_id: str, preparer: SubtaskPreparer) -> None:
        self.agent.add_subtask_preparer(plugin_id, preparer)


def create_app(
    config: NyanpasuConfig | None = None,
    agent: AgentBackend | None = None,
    *,
    plugin_registry: PluginRegistry | None = None,
    session_sources: Callable[[str], SessionSource] | None = None,
) -> FastAPI:
    resolved_config = config or load_config()
    ensure_state_dirs(resolved_config)
    resolved_agent = agent or AgentService(resolved_config)
    runtime = WebPluginRuntime(config=resolved_config, app=None, agent=resolved_agent)
    plugin_manager = PluginManager(resolved_config, runtime, plugin_registry)
    owned_backends = None
    if isinstance(resolved_agent, AgentService):
        backends = resolved_agent.backends
    else:
        owned_backends = backends = Backends(resolved_config)
    session_sources = session_sources or backends.source

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await plugin_manager.setup()
        try:
            await resolved_agent.startup()
            yield
        finally:
            await plugin_manager.shutdown()
            await resolved_agent.shutdown()
            if owned_backends is not None:
                await owned_backends.close()

    app = FastAPI(title="Nyanpasu Agent Service", version="0.1.0", lifespan=lifespan)
    app.state.config = resolved_config
    app.state.agent = resolved_agent
    runtime.app = app
    state_store = StateStore(resolved_config.db_path)
    reader = TranscriptReader(state_store.db_path, session_sources)

    def runtime_info() -> dict[str, Any]:
        info = backends.runtime_info()
        active = info["backends"].get(resolved_config.runtime.backend, {})
        return {"connection": active.get("connection", "idle"), "diagnostics": active.get("diagnostics", []), **info}

    protected = APIRouter(dependencies=[Depends(require_server_token)])
    protected.include_router(dashboard_router(resolved_config, reader, runtime_info))

    @app.middleware("http")
    async def prevent_data_caching(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/dashboard/assets/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RecordNotFound)
    async def missing_record(request, exc: RecordNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc.args[0])})

    @app.exception_handler(SourceUnavailable)
    async def unavailable_source(request, exc: SourceUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(CursorError)
    async def invalid_cursor(request, exc: CursorError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    static_dir = dashboard_static_dir()
    if static_dir is not None:
        app.mount("/dashboard/assets", StaticFiles(directory=static_dir), name="dashboard-assets")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "backend": resolved_config.runtime.backend,
            "enabled_plugins": list(resolved_config.enabled_plugins or resolved_config.plugins),
        }

    @protected.get("/tasks")
    async def tasks(limit: int = 20) -> dict[str, Any]:
        store = StateStore(resolved_config.db_path)
        return {"tasks": [task.model_dump(mode="json") for task in store.recent_tasks(limit)]}

    @protected.get("/contexts")
    async def contexts() -> dict[str, Any]:
        store = StateStore(resolved_config.db_path)
        return {"contexts": [context.model_dump(mode="json") for context in store.list_contexts()]}

    @protected.get("/api/dashboard")
    async def dashboard_api(recent_limit: int = 50, backlog_limit: int = 100) -> dict[str, Any]:
        store = StateStore(resolved_config.db_path)
        snapshot = store.dashboard_snapshot(
            recent_limit=max(1, min(recent_limit, 200)),
            backlog_limit=max(1, min(backlog_limit, 500)),
        )
        return snapshot.model_dump(mode="json")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page() -> HTMLResponse:
        return HTMLResponse(dashboard_html())

    app.include_router(protected)
    return app


def app_from_env() -> FastAPI:
    return create_app(load_config())


DASHBOARD_STATIC_DIR = Path(__file__).parent / "dashboard_static"


def dashboard_html() -> str:
    index = DASHBOARD_STATIC_DIR / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return _dashboard_missing_html()


def dashboard_static_dir() -> Path | None:
    if not (DASHBOARD_STATIC_DIR / "index.html").is_file():
        return None
    return DASHBOARD_STATIC_DIR


def _dashboard_missing_html() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Nyanpasu Dashboard</title>
  </head>
  <body>
    <main>
      <h1>Nyanpasu Dashboard</h1>
      <p>Dashboard assets are not built. Run <code>pnpm run build</code> from the repository root.</p>
    </main>
  </body>
</html>
"""
