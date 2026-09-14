from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nyanpasu.config import EnvCommand
from nyanpasu.environment import resolve_env_value


class GitHubModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class GitHubIntegrationConfig(GitHubModel):
    """GitHub settings with credentials resolved when the plugin starts."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    token: str | None = Field(default=None, repr=False)
    token_env: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None

    @field_validator("token", "token_env")
    @classmethod
    def _nonempty_credential(cls, value: str | None) -> str | None:
        if value is not None and (not value or "\0" in value):
            raise ValueError("GitHub credential settings must be nonempty and contain no NUL")
        return value

    @property
    def resolved_token(self) -> str | None:
        if self.token:
            return self.token
        if self.token_env:
            token = os.getenv(self.token_env)
            if not token:
                raise ValueError(f"integrations.github.token_env: {self.token_env} is missing or empty")
            return token
        return None

    def gh_env(self) -> dict[str, str] | None:
        token = self.resolved_token
        if not token:
            return None
        return {"GH_TOKEN": token, "GITHUB_TOKEN": token}

    def agent_auth_instructions(self) -> tuple[str, ...]:
        if self.token_env:
            if self.token_env in {"GH_TOKEN", "GITHUB_TOKEN"}:
                return (
                    f"GitHub CLI authentication may use `${self.token_env}` if it is exposed to the agent runtime.",
                    "Do not print or otherwise expose authentication environment variable values.",
                )
            return (
                f"If GitHub CLI authentication is needed and `${self.token_env}` is exposed to the agent runtime, "
                f'run `gh` commands with `GH_TOKEN="${{{self.token_env}}}" GITHUB_TOKEN="${{{self.token_env}}}"` '
                "in the command environment.",
                "Do not print or otherwise expose authentication environment variable values.",
            )
        if self.token:
            return (
                "A GitHub token is configured for plugin-side GitHub API calls, but token values are not embedded in "
                "agent prompts. Configure the selected backend’s `env` or `pass_env` for agent GitHub writes.",
            )
        return ("Use ambient `gh auth` for GitHub CLI writes. If authentication is missing, stop and report it.",)


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


def github_integration_from_config(raw: dict[str, Any] | None, *, cwd: Path | None = None) -> GitHubIntegrationConfig:
    settings = dict(raw or {})
    source = settings.get("token")
    if source is not None and settings.get("token_env") is not None:
        raise ValueError("integrations.github: configure either token or token_env")
    command = EnvCommand.model_validate(source) if isinstance(source, dict) else None
    if command is not None:
        settings["token"] = None
    config = GitHubIntegrationConfig.model_validate(settings)
    token = (
        resolve_env_value(command, name="integrations.github.token", cwd=cwd or Path.cwd(), env=dict(os.environ))
        if command is not None
        else config.resolved_token
    )
    return config.model_copy(update={"token": token})


def as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    raise ValueError("expected a string or list of strings")
