from __future__ import annotations

import re
from typing import Any, Literal

from nyanpasu.models import AgentTask, InstructionDocument, TaskAction, WorkspaceRef
from nyanpasu_github.gh import resolve_branch_sha
from nyanpasu_github.instructions import instruction_documents_for_repo
from nyanpasu_github.models import (
    GitHubIntegrationConfig,
    GitHubModel,
    GitHubRepoSettings,
    InstructionDocumentSettings,
)
from nyanpasu_github.pulls import pull_request_number_from_url
from nyanpasu_github.workspace import branch_workspace_ref

PullRequestOutcomeStatus = Literal["published", "no_changes", "dry_run", "failed"]


class GitHubRepoConfigError(ValueError):
    pass


class GitHubBranchTaskContext(GitHubModel):
    repo: str
    settings: GitHubRepoSettings
    base_branch: str
    revision: str
    workspace: WorkspaceRef
    instruction_docs: tuple[InstructionDocument, ...] = ()


class AgenticPullRequestOutcome(GitHubModel):
    status: PullRequestOutcomeStatus
    pr_url: str | None = None
    pr_number: int | None = None
    no_pr_reason: str | None = None
    error: str | None = None


def configured_branch_context(
    *,
    repo: str,
    requested_base_branch: str | None,
    default_base_branch: str,
    repos: dict[str, GitHubRepoSettings],
    plugin_instruction_docs: tuple[InstructionDocumentSettings, ...] = (),
    github: GitHubIntegrationConfig | None = None,
) -> GitHubBranchTaskContext:
    settings = repos.get(repo)
    if settings is None:
        raise GitHubRepoConfigError(f"repository is not configured: {repo}")
    base_branch = requested_base_branch or default_base_branch
    if settings.base_branches and base_branch not in settings.base_branches:
        raise GitHubRepoConfigError(f"base branch is not allowed: {base_branch}")
    revision = resolve_branch_sha(repo, base_branch, env=github.gh_env() if github is not None else None)
    return GitHubBranchTaskContext(
        repo=repo,
        settings=settings,
        base_branch=base_branch,
        revision=revision,
        workspace=branch_workspace_ref(
            repo=repo,
            settings=settings,
            branch=base_branch,
            revision=revision,
        ),
        instruction_docs=instruction_documents_for_repo(
            repo=repo,
            plugin_instruction_docs=plugin_instruction_docs,
            repo_settings=repos,
        ),
    )


def branch_agent_task(
    *,
    task_id: str,
    context_key: str,
    prompt: str,
    branch_context: GitHubBranchTaskContext,
    metadata: dict[str, Any],
    dedupe_key: str | None = None,
) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        action=TaskAction.RUN,
        context_key=context_key,
        prompt=prompt,
        workspace=branch_context.workspace,
        instruction_docs=branch_context.instruction_docs,
        dedupe_key=dedupe_key,
        metadata=metadata,
    )


def parse_agentic_pull_request_outcome(
    final_message: str,
    *,
    existing_pr_url: str | None = None,
    existing_pr_number: int | None = None,
    dry_run: bool = False,
) -> AgenticPullRequestOutcome:
    pr_url = existing_pr_url or _extract_pr_url(final_message)
    pr_number = existing_pr_number or pull_request_number_from_url(pr_url)
    no_pr_reason = _extract_no_pr_reason(final_message)
    if dry_run:
        return AgenticPullRequestOutcome(
            status="dry_run",
            pr_url=pr_url,
            pr_number=pr_number,
            no_pr_reason=no_pr_reason,
        )
    if pr_url and pr_number is not None:
        return AgenticPullRequestOutcome(status="published", pr_url=pr_url, pr_number=pr_number)
    if no_pr_reason is not None:
        return AgenticPullRequestOutcome(status="no_changes", no_pr_reason=no_pr_reason)
    return AgenticPullRequestOutcome(status="failed", error="agent final message did not include `PR: <url>`")


def _extract_pr_url(text: str) -> str | None:
    marker = re.search(r"(?im)^\s*PR:\s*(https://github\.com/[^\s]+/[^\s]+/pull/\d+)\s*$", text)
    if marker is not None:
        return marker.group(1)
    fallback = re.search(r"https://github\.com/[^\s]+/[^\s]+/pull/\d+", text)
    return fallback.group(0) if fallback is not None else None


def _extract_no_pr_reason(text: str) -> str | None:
    marker = re.search(r"(?im)^\s*NO_PR:\s*(.+?)\s*$", text)
    if marker is None:
        return None
    return marker.group(1).strip() or "no pull request created"
