from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nyanpasu.claude import ClaudeBackend
from nyanpasu.codex import CodexAppServerBackend
from nyanpasu.environment import process_env
from nyanpasu.transcript.claude import ClaudeHistorySource
from nyanpasu.transcript.codex import CodexHistorySource

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.execution import ExecutionBackend
    from nyanpasu.isolation import ExecutionIsolation
    from nyanpasu.models import NativeSessionLocation
    from nyanpasu.transcript.history import SessionSource


@dataclass(frozen=True)
class Backend:
    execution: ExecutionBackend
    history: SessionSource


class Backends:
    """Own lazily constructed runtimes, including providers of older sessions."""

    def __init__(self, config: NyanpasuConfig, instances: dict[str, Backend] | None = None):
        self.config = config
        self._instances = dict(instances or {})
        self._provided = set(self._instances)
        self._history_limit = asyncio.Semaphore(2)
        self._active: dict[int, tuple[str, Backend]] = {}
        self._latest: dict[str, dict] = {}
        self._session_home: Callable[[str, str], NativeSessionLocation] | None = None

    def register_session_locator(self, locate: Callable[[str, str], NativeSessionLocation]) -> None:
        self._session_home = locate

    def _create(self, name: str, *, native_home: Path | None = None, isolation=None, cwd=None) -> Backend:
        configured = self.config.backends[name]
        config = self.config
        if native_home is not None:
            field = "CODEX_HOME" if configured.driver == "codex" else "CLAUDE_CONFIG_DIR"
            configured = configured.model_copy(
                update={
                    "process": configured.process.model_copy(
                        update={"env": {**configured.process.env, field: str(native_home)}}
                    )
                }
            )
            config = config.model_copy(update={"backends": {**config.backends, name: configured}})
        if configured.driver == "codex":
            codex = CodexAppServerBackend(config, name, isolation=isolation, cwd=cwd)
            return Backend(
                codex,
                CodexHistorySource(codex, native_home=native_home, root=isolation.home if isolation else native_home),
            )
        claude = ClaudeBackend(config, name, isolation=isolation)
        return Backend(claude, ClaudeHistorySource(claude.env, root=isolation.home if isolation else native_home))

    def get(self, name: str) -> Backend:
        if name not in self._instances:
            self._instances[name] = self._create(name)
        return self._instances[name]

    def source(self, name: str) -> SessionSource:
        if name in self._provided:
            return self.get(name).history
        return RoutedHistory(self, name)

    @asynccontextmanager
    async def history(self, name: str, thread_id: str):
        from nyanpasu.isolation import ExecutionIsolation

        if self._session_home is None:
            raise RuntimeError("native session locator is not registered")
        location = self._session_home(name, thread_id)
        if self.config.backends[name].driver != location.driver:
            raise ValueError("registered native session driver differs from backend configuration")
        home, isolated_home = location.native_home, location.isolated_home
        cwd = self.config.state_dir / "history-readers" / hashlib.sha256(str(home).encode()).hexdigest()
        cwd.mkdir(parents=True, exist_ok=True)
        isolation = ExecutionIsolation(home=isolated_home, readonly_paths=self.config.isolation.readonly_paths)
        async with self._history_limit:
            backend = self._create(name, native_home=home, isolation=isolation, cwd=cwd)
            try:
                yield backend
            finally:
                await backend.execution.close()

    @asynccontextmanager
    async def turn(self, name: str, *, isolation: ExecutionIsolation, cwd: Path):
        if name in self._provided:
            yield self.get(name).execution
            return
        from nyanpasu.isolation import seed_home

        configured = self.config.backends[name]
        env = process_env(configured.process, cwd=self.config.state_dir, backend=configured.driver)
        native_home = seed_home(
            isolation.home,
            configured.driver,
            env,
            native_directory=configured.home.native_directory,
            template=configured.home.template,
        )
        backend = self._create(name, native_home=native_home, isolation=isolation, cwd=cwd)
        self._active[id(backend)] = (name, backend)
        try:
            yield backend.execution
        finally:
            await backend.execution.close()
            self._latest[name] = backend.execution.runtime_info()
            self._active.pop(id(backend), None)

    async def cleanup_session(self, name: str, thread_id: str) -> None:
        if name in self._provided:
            await self.get(name).execution.cleanup_thread(thread_id)
            return
        async with self.history(name, thread_id) as backend:
            await backend.execution.cleanup_thread(thread_id)

    def _runtime_info(self, name: str) -> dict:
        active = [backend for backend_name, backend in self._active.values() if backend_name == name]
        if active:
            return {
                **active[-1].execution.runtime_info(),
                "diagnostics": [
                    entry for backend in active for entry in backend.execution.runtime_info().get("diagnostics", [])
                ][-100:],
            }
        if name in self._instances:
            return self._instances[name].execution.runtime_info()
        return self._latest.get(name, {"connection": "idle", "diagnostics": []})

    def runtime_info(self) -> dict:
        return {
            "backends": {
                name: {
                    "driver": configured.driver,
                    "bin": configured.process.command[0],
                    "defaults": configured.defaults.model_dump(),
                    "active_turns": sum(backend_name == name for backend_name, _ in self._active.values()),
                    **self._runtime_info(name),
                }
                for name, configured in self.config.backends.items()
            }
        }

    async def close(self) -> None:
        for backend in [
            *self._instances.values(),
            *(item[1] for item in self._active.values()),
        ]:
            await backend.execution.close()


class RoutedHistory:
    def __init__(self, backends: Backends, name: str):
        self.backends = backends
        self.name = name

    async def read_metadata(self, thread_id: str):
        async with self.backends.history(self.name, thread_id) as backend:
            metadata = await backend.history.read_metadata(thread_id)
            return metadata.model_copy(update={"backend": self.name})

    async def read_session(self, thread_id: str):
        async with self.backends.history(self.name, thread_id) as backend:
            history = await backend.history.read_session(thread_id)
            return history.model_copy(update={"metadata": history.metadata.model_copy(update={"backend": self.name})})
