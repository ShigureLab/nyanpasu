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
            if name == "codex":
                codex = CodexAppServerBackend(self.config)
                backend = Backend(codex, CodexHistorySource(codex))
            elif name == "claude":
                claude = ClaudeBackend(self.config)
                backend = Backend(claude, ClaudeHistorySource(claude.env))
            else:
                raise ValueError(f"unknown runtime backend: {name}")
            self._instances[name] = backend
        return self._instances[name]

    def source(self, name: str) -> SessionSource:
        return self.get(name).history

    def runtime_info(self) -> dict:
        return {
            "backends": {
                name: {"bin": self.config.process_config(name).bin, **backend.execution.runtime_info()}
                for name, backend in self._instances.items()
            }
        }

    async def close(self) -> None:
        for backend in self._instances.values():
            await backend.execution.close()
