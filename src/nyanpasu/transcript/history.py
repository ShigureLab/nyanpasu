from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from nyanpasu.transcript.models import EntryUpdate


def session_key(backend: str, thread_id: str) -> str:
    # Preserve existing Codex deep links. Other providers have separate namespaces.
    return thread_id if backend == "codex" else f"{backend}:{thread_id}"


class SessionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    backend: str
    cwd: str | None = None
    model: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None
    cli_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class HistoryItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    presentation: EntryUpdate
    raw: dict[str, Any]
    redacted: bool = False
    started_at: str | None = None
    completed_at: str | None = None
    recorded_at: str | None = None


class HistoryTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    items: tuple[HistoryItem, ...] = ()


class SessionHistory(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: SessionMetadata
    turns: tuple[HistoryTurn, ...] = ()


class SessionSource(Protocol):
    async def read_session(self, thread_id: str) -> SessionHistory: ...
