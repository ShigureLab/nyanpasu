from __future__ import annotations

from typing import TYPE_CHECKING

from nyanpasu.models import WorkspaceRef

if TYPE_CHECKING:
    from nyanpasu_github.models import GitHubRepoSettings, PullRequestRef


def repo_workspace_ref(
    *,
    repo: str,
    settings: GitHubRepoSettings,
    ref: str | None = None,
    revision: str | None = None,
) -> WorkspaceRef:
    return WorkspaceRef(
        key=repo,
        local_path=settings.local_path,
        remote=settings.github_remote,
        ref=ref,
        revision=revision,
    )


def branch_workspace_ref(
    *,
    repo: str,
    settings: GitHubRepoSettings,
    branch: str,
    revision: str | None = None,
) -> WorkspaceRef:
    return repo_workspace_ref(
        repo=repo,
        settings=settings,
        ref=f"refs/heads/{branch}",
        revision=revision,
    )


def pull_request_workspace_ref(pr: PullRequestRef, settings: GitHubRepoSettings) -> WorkspaceRef:
    return repo_workspace_ref(
        repo=pr.repo,
        settings=settings,
        ref=f"pull/{pr.number}/head",
        revision=pr.head_sha or None,
    )
