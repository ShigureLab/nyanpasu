from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class GitHubModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class GitHubIntegrationConfig(GitHubModel):
    token: str | None = None
    token_env: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None

    @property
    def resolved_token(self) -> str | None:
        if self.token:
            return self.token
        if self.token_env:
            return os.getenv(self.token_env)
        return None

    def gh_env(self) -> dict[str, str] | None:
        token = self.resolved_token
        if not token:
            return None
        return {"GH_TOKEN": token, "GITHUB_TOKEN": token}


class InstructionDocumentSettings(GitHubModel):
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


class GitHubRepoSettings(GitHubModel):
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
        return as_str_tuple(value)


class GitHubRepoConfig(GitHubModel):
    repo: str
    local_path: Path
    github_remote: str | None = None
    base_branches: tuple[str, ...] = ()

    @field_validator("local_path", mode="before")
    @classmethod
    def _local_path(cls, value: Any) -> Path:
        return Path(value).expanduser().resolve()


class PullRequestRef(GitHubModel):
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


def repo_configs_from_settings(repos: dict[str, GitHubRepoSettings]) -> dict[str, GitHubRepoConfig]:
    return {
        repo: GitHubRepoConfig(
            repo=repo,
            local_path=settings.local_path,
            github_remote=settings.github_remote,
            base_branches=settings.base_branches,
        )
        for repo, settings in repos.items()
    }


def github_integration_from_config(raw: dict[str, Any] | None) -> GitHubIntegrationConfig:
    return GitHubIntegrationConfig.model_validate(raw or {})


def as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    raise ValueError("expected a string or list of strings")
