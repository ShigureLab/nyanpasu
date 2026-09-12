from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class ContentUpdate(BaseModel):
    block_id: str
    kind: str
    text: str
    append: bool = False


class EntryUpdate(BaseModel):
    """Adapter output; omitted fields preserve the existing projection."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = None
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
    missing_parts: list[str] = Field(default_factory=list)
    blocks: list[ContentUpdate] = Field(default_factory=list)

    def fields(self) -> dict:
        return self.model_dump(
            exclude_unset=True,
            exclude_none=True,
            exclude={"key", "blocks", "source_truncated", "missing_parts"},
        )


class Coverage(ContractModel):
    source_truncated: bool = False
    capture_gap: bool = False
    redacted: bool = False
    missing_parts: list[str] = Field(default_factory=list)


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
    source_timestamp: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    source: Source
    source_item_id: str | None = None
    related_entry_ids: list[str] = Field(default_factory=list)
    blocks: list[TranscriptBlock] = Field(default_factory=list)
    raw_event_count: int = 0
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


class TranscriptContract(ContractModel):
    window: TranscriptWindow
    changes: TranscriptChanges
