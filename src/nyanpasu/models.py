from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class TaskAction(StrEnum):
    RUN = "run"
    CLEANUP = "cleanup"
    IGNORED = "ignored"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class NyanpasuModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class WorkspaceRef(NyanpasuModel):
    key: str
    local_path: Path
    remote: str | None = None
    ref: str | None = None
    revision: str | None = None

    @field_validator("local_path", mode="before")
    @classmethod
    def _local_path(cls, value: Any) -> Path:
        return Path(value).expanduser().resolve()


class InstructionDocument(NyanpasuModel):
    name: str
    content: str
    source: str | None = None


class AgentTask(NyanpasuModel):
    task_id: str
    action: TaskAction
    context_key: str
    prompt: str
    workspace: WorkspaceRef | None = None
    instruction_docs: tuple[InstructionDocument, ...] = ()
    dedupe_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    workspace_policy: Literal["context", "event_snapshot"] = "context"
    cleanup_policy: Literal["context", "none"] = "none"

    @property
    def key(self) -> str:
        return self.dedupe_key or self.task_id


class AgentContext(NyanpasuModel):
    context_key: str
    thread_id: str | None
    session_worktree: Path | None
    workspace_key: str | None
    revision: str | None

    @field_validator("session_worktree", mode="before")
    @classmethod
    def _session_worktree(cls, value: Any) -> Path | None:
        if value is None or value == "":
            return None
        return Path(value).expanduser().resolve()


class TaskRunResult(NyanpasuModel):
    task_id: str
    status: TaskStatus
    thread_id: str | None
    turn_id: str | None
    final_message: str
    raw_events: list[dict[str, Any]]
    event_worktree: Path | None = None
    session_worktree: Path | None = None
    error: str | None = None

    @field_validator("event_worktree", "session_worktree", mode="before")
    @classmethod
    def _path_or_none(cls, value: Any) -> Path | None:
        if value is None or value == "":
            return None
        return Path(value).expanduser().resolve()

    @field_serializer("status")
    def _serialize_status(self, status: TaskStatus) -> str:
        return status.value


class CodexRunResult(NyanpasuModel):
    thread_id: str
    turn_id: str | None
    final_message: str
    raw_events: list[dict[str, Any]]


class TaskRunSummary(NyanpasuModel):
    task_id: str
    dedupe_key: str | None = None
    context_key: str
    action: TaskAction
    status: TaskStatus
    event_worktree: Path | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    error: str | None = None
    created_at: float | None = None
    updated_at: float

    @field_validator("event_worktree", mode="before")
    @classmethod
    def _event_worktree(cls, value: Any) -> Path | None:
        if value is None or value == "":
            return None
        return Path(value).expanduser().resolve()

    @field_serializer("action", "status")
    def _serialize_enum(self, value: StrEnum) -> str:
        return value.value


class CoalescedTaskRecord(NyanpasuModel):
    task_id: str
    task: dict[str, Any]
    created_at: float


class ContextLease(NyanpasuModel):
    context_key: str
    owner_id: str
    task_id: str
    acquired_at: float
    heartbeat_at: float
    expires_at: float


class DashboardTotals(NyanpasuModel):
    total: int
    queued: int
    running: int
    completed: int
    failed: int
    backlog: int
    contexts: int
    active_leases: int


class DashboardPluginSummary(NyanpasuModel):
    plugin_id: str
    total: int = 0
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    last_updated_at: float | None = None


class DashboardTaskItem(NyanpasuModel):
    task_id: str
    dedupe_key: str | None = None
    plugin_id: str
    action: str
    status: str
    context_key: str
    title: str
    source: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    error: str | None = None
    created_at: float
    updated_at: float
    age_seconds: float


class DashboardSnapshot(NyanpasuModel):
    generated_at: float
    totals: DashboardTotals
    status_counts: dict[str, int]
    action_counts: dict[str, int]
    plugins: tuple[DashboardPluginSummary, ...]
    backlog: tuple[DashboardTaskItem, ...]
    recent: tuple[DashboardTaskItem, ...]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")
