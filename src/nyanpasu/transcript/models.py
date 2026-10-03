from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from nyanpasu.memory import MemoryNavigation, MemorySource
from nyanpasu.targets import ExecutionTarget


class ContractModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class TaskExecution(ContractModel):
    kind: str
    execution: ExecutionTarget | None


class ContentUpdate(BaseModel):
    block_id: str
    kind: str
    text: str


class EntryUpdate(BaseModel):
    """Presentation fields derived from one Codex item."""

    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    title: str | None = None
    state: str | None = None
    raw_state: str | None = None
    phase: str | None = None
    source_item_id: str | None = None
    tool_name: str | None = None
    command: str | None = None
    cwd: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    decision: str | None = None
    source_truncated: bool = False
    blocks: list[ContentUpdate] = Field(default_factory=list)

    def fields(self) -> dict:
        return self.model_dump(
            exclude_unset=True,
            exclude_none=True,
            exclude={"blocks", "source_truncated"},
        )


class Coverage(ContractModel):
    source_truncated: bool = False
    redacted: bool = False


class SessionMetadata(ContractModel):
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


class Session(TaskExecution):
    session_id: str
    context_key: str
    title: str
    thread_id: str | None
    state: str
    backend: str
    created_at: str
    updated_at: str
    task_count: int
    spawned_by_task_id: str | None
    execution_uncertain: bool
    coverage: Coverage
    origin: str
    previous_session_id: str | None


class SessionPage(ContractModel):
    items: list[Session]
    total: int
    offset: int
    has_more: bool


class Turn(TaskExecution):
    task_id: str
    turn_id: str | None
    title: str
    state: str
    cwd: str | None
    revision: str | None
    created_at: str
    ended_at: str | None


class SessionDetail(Session):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    tasks: list[Turn]
    has_more_tasks: bool
    runtime: SessionMetadata | None
    history_error: str | None = None


class Source(ContractModel):
    backend: str
    version: str | None = None
    origin: str = "native"


class TranscriptBlock(ContractModel):
    block_id: str
    kind: str
    mime_type: str = "text/plain"
    preview: str
    preview_truncated: bool
    content_ref: str
    recorded_bytes: int


class TranscriptEntry(ContractModel):
    entry_id: str
    session_id: str
    task_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    first_seq: str
    revision_seq: str
    kind: str
    title: str
    phase: str | None = None
    state: str = "unknown"
    raw_state: str | None = None
    observed_at: str
    started_at: str | None = None
    completed_at: str | None = None
    recorded_at: str | None = None
    source: Source
    source_item_id: str | None = None
    blocks: list[TranscriptBlock] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    tool_name: str | None = None
    command: str | None = None
    cwd: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    decision: str | None = None


class TranscriptWindow(ContractModel):
    session_id: str
    generation: str
    generated_at: str
    entries: list[TranscriptEntry]
    before_cursor: str | None
    after_window_cursor: str | None
    has_older: bool
    has_newer: bool
    change_cursor: str
    coverage: Coverage


class EntryChange(ContractModel):
    seq: str
    upserts: list[TranscriptEntry]


class TranscriptChanges(ContractModel):
    session_id: str
    generation: str
    generated_at: str
    changes: list[EntryChange]
    next_cursor: str
    has_more: bool


class TaskLink(ContractModel):
    task_id: str
    session_id: str | None
    title: str


class TaskArtifact(ContractModel):
    name: str
    sha256: str
    bytes: int


class TaskResultArtifact(TaskArtifact):
    path: str


class TaskEvidence(ContractModel):
    summary: str
    artifacts: list[TaskResultArtifact]
    data: dict[str, object]


class TaskRecord(TaskExecution):
    task_id: str
    context_key: str
    status: str
    action: str
    backend: str
    session_id: str | None
    coalesced_into: str | None
    spawned_by_task_id: str | None
    context_generation: int
    error: str | None
    created_at: float
    updated_at: float


class Task(TaskRecord):
    title: str
    plugin_id: str


class TaskPage(ContractModel):
    items: list[Task]
    total: int
    has_more: bool


class TaskChild(TaskExecution):
    task_id: str
    status: str
    context_key: str
    backend: str


class TaskDetail(TaskRecord):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    dedupe_key: str | None
    entry_id: str | None
    turn_id: str | None
    thread_id: str | None
    event_worktree: str | None
    task: dict[str, object]
    session_thread_id: str | None
    session_backend: str
    lease_expires_at: float | None
    lifecycle: str
    waiting_for: list[str]
    children: list[TaskChild]
    subtask_result: TaskEvidence | None
    history_error: str | None = None


class TaskTreeNode(TaskLink, TaskExecution):
    backend: str
    purpose: str | None
    status: str
    created_at: str
    waiting: bool
    summary: str | None
    error: str | None
    artifacts: list[TaskArtifact]
    children: list[TaskTreeNode]


class SessionTaskTree(ContractModel):
    parent: TaskLink | None
    groups: list[TaskTreeNode]
    has_more: bool


class SearchHit(ContractModel):
    entry_id: str
    task_id: str | None
    title: str
    snippet: str
    block_id: str
    content_ref: str
    offset: int


class SearchResults(ContractModel):
    items: list[SearchHit]
    has_more: bool


class ContentPage(ContractModel):
    text: str
    content_ref: str
    offset: int
    next_offset: int | None
    recorded_bytes: int


class MemorySourceSummary(ContractModel):
    id: str
    domain: str
    task_id: str
    title: str
    topics: list[str]
    sources: list[str]
    revision: str
    updated_at: str
    complete: bool


class MemoryDomain(ContractModel):
    id: str
    count: int


class MemoryTopic(ContractModel):
    name: str
    count: int


class MemoryPage(ContractModel):
    enabled: bool
    count: int
    domains: list[MemoryDomain]
    topics: list[MemoryTopic]
    items: list[MemorySourceSummary]
    navigation_count: int
    navigation: list[MemoryNavigation]
    has_more: bool


class TranscriptContract(ContractModel):
    sessions: SessionPage
    session: SessionDetail
    tasks: TaskPage
    task: TaskDetail
    window: TranscriptWindow
    changes: TranscriptChanges
    task_tree: SessionTaskTree
    search: SearchResults
    content: ContentPage
    memory: MemoryPage
    memory_source: MemorySource
