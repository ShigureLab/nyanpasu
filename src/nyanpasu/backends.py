from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nyanpasu.claude import ClaudeBackend
from nyanpasu.codex import CodexAppServerBackend
from nyanpasu.resources import ProcessResources
from nyanpasu.transcript.claude import ClaudeHistorySource
from nyanpasu.transcript.codex import CodexHistorySource

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.execution import ExecutionBackend
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
        self._active: dict[int, tuple[str, Backend, str]] = {}
        self._latest: dict[str, dict] = {}
        self._resources = ProcessResources()

    def _create(self, name: str) -> Backend:
        configured = self.config.backends[name]
        if configured.driver == "codex":
            codex = CodexAppServerBackend(self.config, name)
            native_home = Path(
                codex._env.get("CODEX_HOME") or Path(codex._env.get("HOME", str(Path.home()))) / ".codex"
            )
            return Backend(codex, CodexHistorySource(codex, native_home=native_home, root=native_home))
        claude = ClaudeBackend(self.config, name)
        return Backend(claude, ClaudeHistorySource(claude.env))

    def get(self, name: str) -> Backend:
        if name not in self._instances:
            self._instances[name] = self._create(name)
        return self._instances[name]

    def source(self, name: str) -> SessionSource:
        if name in self._provided:
            return self.get(name).history
        return RoutedHistory(self, name)

    @asynccontextmanager
    async def history(self, name: str):
        async with self._history_limit:
            async with self._observe(name, self._create(name), activity="history") as backend:
                yield backend

    @asynccontextmanager
    async def turn(self, name: str):
        if name in self._provided:
            yield self.get(name).execution
            return
        async with self._observe(name, self._create(name), activity="turn") as backend:
            yield backend.execution

    @asynccontextmanager
    async def _observe(self, name: str, backend: Backend, *, activity: str):
        self._active[id(backend)] = (name, backend, activity)
        try:
            yield backend
        finally:
            await backend.execution.close()
            if activity == "turn":
                self._latest[name] = backend.execution.runtime_info()
            self._active.pop(id(backend), None)

    async def archive_sessions(self, sessions: set[tuple[str, str]]) -> None:
        for name in sorted({name for name, _ in sessions}):
            thread_ids = sorted(thread_id for backend, thread_id in sessions if backend == name)
            if name in self._provided:
                for thread_id in thread_ids:
                    await self.get(name).execution.archive(thread_id)
            else:
                async with self.history(name) as backend:
                    for thread_id in thread_ids:
                        await backend.execution.archive(thread_id)

    def _runtime_info(self, name: str, active: list[tuple[str, Backend, str]]) -> dict:
        backends = [backend for backend_name, backend, _ in active if backend_name == name]
        if name in self._instances:
            backends.insert(0, self._instances[name])
        if backends:
            observed = [backend.execution.runtime_info() for backend in backends]
            latest = next((info for info in reversed(observed) if info.get("pids")), observed[-1])
            return {
                **latest,
                "pids": sorted({pid for info in observed for pid in info.get("pids", [])}),
                "diagnostics": [entry for info in observed for entry in info.get("diagnostics", [])][-100:],
            }
        return {**self._latest.get(name, {"connection": "idle", "diagnostics": []}), "pids": []}

    def runtime_info(self) -> dict:
        active = list(self._active.values())
        observed = {name: self._runtime_info(name, active) for name in self.config.backends}
        resources = self._resources.sample({name: info.get("pids", []) for name, info in observed.items()})
        return {
            "backends": {
                name: {
                    "driver": configured.driver,
                    "bin": configured.process.command[0],
                    "defaults": configured.defaults.model_dump(),
                    "active_turns": sum(
                        backend_name == name and activity == "turn" for backend_name, _, activity in active
                    ),
                    **observed[name],
                    "resources": resources[name],
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
        async with self.backends.history(self.name) as backend:
            metadata = await backend.history.read_metadata(thread_id)
            return metadata.model_copy(update={"backend": self.name})

    async def read_session(self, thread_id: str):
        async with self.backends.history(self.name) as backend:
            history = await backend.history.read_session(thread_id)
            return history.model_copy(update={"metadata": history.metadata.model_copy(update={"backend": self.name})})
