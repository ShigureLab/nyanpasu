from __future__ import annotations

from typing import Any

from nyanpasu_github.models import GitHubRepoSettings, InstructionDocumentSettings, as_str_tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitHubPrMakerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    repos: dict[str, GitHubRepoSettings] = Field(default_factory=dict)
    instruction_docs: tuple[InstructionDocumentSettings, ...] = ()
    branch_prefix: str = "nyanpasu"
    default_base_branch: str = "main"
    dry_run: bool = False
    draft: bool = False
    follow_up_enabled: bool = True
    follow_up_interval_seconds: int = 600
    git_author_name: str | None = None
    git_author_email: str | None = None
    extra_prompt: str = ""


class CreatePullRequestTaskRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    task: str
    base_branch: str | None = None
    title: str | None = None
    body: str | None = None
    branch_name: str | None = None
    commit_message: str | None = None
    context_key: str | None = None
    task_id: str | None = None
    labels: tuple[str, ...] = ()
    draft: bool | None = None
    dry_run: bool | None = None

    @field_validator("labels", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> tuple[str, ...]:
        return as_str_tuple(value)


class PullRequestPublishMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    base_branch: str
    branch_name: str
    title: str
    body: str
    commit_message: str
    task: str
    context_key: str
    labels: tuple[str, ...] = ()
    draft: bool
    dry_run: bool
    existing_pr_number: int | None = None
    existing_pr_url: str | None = None
    remote_url: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None
