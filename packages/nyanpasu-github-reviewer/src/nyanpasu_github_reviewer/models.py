from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewAction(StrEnum):
    REVIEW = "review"
    CLEANUP = "cleanup"
    IGNORED = "ignored"


class GitHubReviewerModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class RepoConfig(GitHubReviewerModel):
    repo: str
    local_path: Path
    github_remote: str | None = None
    base_branches: tuple[str, ...] = ()

    @field_validator("local_path", mode="before")
    @classmethod
    def _local_path(cls, value: Any) -> Path:
        return Path(value).expanduser().resolve()


class PullRequestRef(GitHubReviewerModel):
    repo: str
    number: int
    url: str
    base_ref: str
    head_ref: str
    head_sha: str
    state: str
    draft: bool

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


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


class PollCycleResult(GitHubReviewerModel):
    submitted: int
    duplicates: int
    ignored: int
    baselined: int
    repos: tuple[str, ...]


class InstructionDocumentSettings(GitHubReviewerModel):
    name: str | None = None
    path: Path | None = None
    content: str | None = None
    required: bool = True

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> Path | None:
        if value is None or value == "":
            return None
        return Path(value).expanduser().resolve()

    @model_validator(mode="after")
    def _source_required(self) -> InstructionDocumentSettings:
        if self.path is None and not self.content:
            raise ValueError("instruction document requires either path or content")
        return self


class RepoSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    local_path: Path
    github_remote: str | None = None
    base_branches: tuple[str, ...] = ()
    instruction_docs: tuple[InstructionDocumentSettings, ...] = ()

    @field_validator("local_path", mode="before")
    @classmethod
    def _local_path(cls, value: Any) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("base_branches", mode="before")
    @classmethod
    def _base_branches(cls, value: Any) -> tuple[str, ...]:
        return _as_str_tuple(value)


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
        return _as_str_tuple(value)

    @property
    def repo_configs(self) -> dict[str, RepoConfig]:
        return {
            repo: RepoConfig(
                repo=repo,
                local_path=settings.local_path,
                github_remote=settings.github_remote,
                base_branches=settings.base_branches,
            )
            for repo, settings in self.repos.items()
        }


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    raise ValueError("expected a string or list of strings")
