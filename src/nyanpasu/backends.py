from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nyanpasu.claude import ClaudeBackend
from nyanpasu.codex import CodexAppServerBackend
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

    def get(self, name: str) -> Backend:
        if name not in self._instances:
            configured = self.config.backends[name]
            if configured.driver == "codex":
                codex = CodexAppServerBackend(self.config, name)
                backend = Backend(codex, CodexHistorySource(codex))
            else:
                claude = ClaudeBackend(self.config, name)
                backend = Backend(claude, ClaudeHistorySource(claude.env))
            self._instances[name] = backend
        return self._instances[name]

    def source(self, name: str) -> SessionSource:
        return self.get(name).history

    def runtime_info(self) -> dict:
        return {
            "backends": {
                name: {
                    "driver": configured.driver,
                    "bin": configured.process.command[0],
                    "defaults": configured.defaults.model_dump(),
                    **(
                        self._instances[name].execution.runtime_info()
                        if name in self._instances
                        else {"connection": "idle", "diagnostics": []}
                    ),
                }
                for name, configured in self.config.backends.items()
            }
        }

    async def close(self) -> None:
        for backend in self._instances.values():
            await backend.execution.close()
