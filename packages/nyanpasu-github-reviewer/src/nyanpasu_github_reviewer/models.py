from __future__ import annotations

from enum import StrEnum
from typing import Any

from nyanpasu_github.models import (
    GitHubRepoConfig as RepoConfig,
    GitHubRepoSettings as RepoSettings,
    InstructionDocumentSettings,
    PullRequestRef,
    as_str_tuple,
    repo_configs_from_settings,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReviewAction(StrEnum):
    REVIEW = "review"
    CLEANUP = "cleanup"
    IGNORED = "ignored"


class GitHubReviewerModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReviewEvent(GitHubReviewerModel):
    delivery_id: str
    github_event: str
    action: ReviewAction
    pr: PullRequestRef | None
    after_sha: str | None
    raw: dict[str, Any]


class PollEventCursor(GitHubReviewerModel):
    repo: str
    last_event_created_at: str
    cursor_event_ids: tuple[str, ...] = ()
    initialized_at: float
    updated_at: float


class PullRequestUpdatedCursor(GitHubReviewerModel):
    repo: str
    last_updated_at: str
    pr_node_ids: tuple[str, ...] = ()
    initialized_at: float
    updated_at: float


class PullRequestTimelineCursor(GitHubReviewerModel):
    repo: str
    pr_number: int
    last_item_updated_at: str
    item_ids: tuple[str, ...] = ()
    initialized_at: float
    updated_at: float


class PullRequestSnapshot(GitHubReviewerModel):
    repo: str
    number: int
    node_id: str
    url: str
    state: str
    draft: bool
    base_ref: str
    head_ref: str
    head_repo: str
    head_sha: str
    title_hash: str
    body_hash: str
    created_at: str
    updated_at: str

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


class GitHubEventJournalStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class GitHubEventJournalRecord(GitHubReviewerModel):
    delivery_id: str
    dedupe_key: str
    source: str
    repo: str
    pr_number: int | None = None
    github_event: str
    action: ReviewAction
    event_created_at: str
    event: ReviewEvent
    status: GitHubEventJournalStatus = GitHubEventJournalStatus.PENDING
    result_json: str | None = None
    error: str | None = None
    created_at: float
    updated_at: float


class PollCycleResult(GitHubReviewerModel):
    submitted: int
    duplicates: int
    ignored: int
    baselined: int
    repos: tuple[str, ...]


class GitHubReviewerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    repos: dict[str, RepoSettings] = Field(default_factory=dict)
    github_login: str | None = None
    webhook_secret: str | None = None
    gh_llm_bin: str = "gh-llm"
    agent_name: str = "Nyanpasu"
    instruction_docs: tuple[InstructionDocumentSettings, ...] = ()
    review_language: str = "Chinese"
    auto_collapse_author_logins: tuple[str, ...] = ()
    poll_enabled: bool = True
    poll_interval_seconds: int = 600
    poll_event_pages: int = 3
    poll_max_events_per_cycle: int = 0
    dry_run: bool = False
    post_reviews: bool = True
    request_changes_on_findings: bool = True

    @field_validator("auto_collapse_author_logins", mode="before")
    @classmethod
    def _auto_collapse_author_logins(cls, value: Any) -> tuple[str, ...]:
        return as_str_tuple(value)

    @property
    def repo_configs(self) -> dict[str, RepoConfig]:
        return repo_configs_from_settings(self.repos)
