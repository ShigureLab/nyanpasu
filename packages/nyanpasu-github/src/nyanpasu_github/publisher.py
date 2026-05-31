from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from nyanpasu_github.gh import run_gh, run_git
from nyanpasu_github.pulls import pull_request_number_from_url

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


class PublishPullRequestRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    base_branch: str
    branch_name: str
    title: str
    body: str
    commit_message: str
    labels: tuple[str, ...] = ()
    draft: bool = False
    dry_run: bool = False
    existing_pr_number: int | None = None
    existing_pr_url: str | None = None
    remote_url: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None


class PublishPullRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["published", "dry_run", "no_changes"]
    branch_name: str
    changed_files: tuple[str, ...] = ()
    commit_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None


class PullRequestPublisher(Protocol):
    def publish(self, *, worktree: Path, request: PublishPullRequestRequest) -> PublishPullRequestResult: ...


class GitHubPullRequestPublisher:
    def __init__(
        self,
        *,
        git_runner: GitRunner = run_git,
        gh_runner: GitRunner = run_gh,
    ) -> None:
        self.git_runner = git_runner
        self.gh_runner = gh_runner

    def publish(self, *, worktree: Path, request: PublishPullRequestRequest) -> PublishPullRequestResult:
        changed_files = _changed_files(self.git_runner(["status", "--porcelain"], cwd=worktree).stdout)
        if not changed_files:
            return PublishPullRequestResult(status="no_changes", branch_name=request.branch_name)
        if request.dry_run:
            return PublishPullRequestResult(
                status="dry_run",
                branch_name=request.branch_name,
                changed_files=changed_files,
            )

        remote = _remote_name_for_url(worktree, request.remote_url, self.git_runner)
        env = _commit_env(request)
        self.git_runner(["switch", "-C", request.branch_name], cwd=worktree)
        self.git_runner(["add", "-A"], cwd=worktree)
        self.git_runner(["commit", "-m", request.commit_message], cwd=worktree, env=env)
        commit_sha = self.git_runner(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
        self.git_runner(
            ["push", "--force-with-lease", "--set-upstream", remote, f"HEAD:refs/heads/{request.branch_name}"],
            cwd=worktree,
        )
        if request.existing_pr_number is not None:
            return PublishPullRequestResult(
                status="published",
                branch_name=request.branch_name,
                changed_files=changed_files,
                commit_sha=commit_sha,
                pr_url=request.existing_pr_url,
                pr_number=request.existing_pr_number,
            )
        gh_args = [
            "pr",
            "create",
            "--repo",
            request.repo,
            "--base",
            request.base_branch,
            "--head",
            request.branch_name,
            "--title",
            request.title,
            "--body",
            request.body,
        ]
        for label in request.labels:
            gh_args.extend(["--label", label])
        if request.draft:
            gh_args.append("--draft")
        pr_url = self.gh_runner(gh_args, cwd=worktree).stdout.strip().splitlines()[-1]
        return PublishPullRequestResult(
            status="published",
            branch_name=request.branch_name,
            changed_files=changed_files,
            commit_sha=commit_sha,
            pr_url=pr_url,
            pr_number=pull_request_number_from_url(pr_url),
        )


def _changed_files(status_output: str) -> tuple[str, ...]:
    files: list[str] = []
    for line in status_output.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.rsplit(" -> ", maxsplit=1)[-1]
        files.append(path)
    return tuple(files)


def _commit_env(request: PublishPullRequestRequest) -> Mapping[str, str] | None:
    if not request.git_author_name and not request.git_author_email:
        return None
    env = dict(os.environ)
    if request.git_author_name:
        env["GIT_AUTHOR_NAME"] = request.git_author_name
        env["GIT_COMMITTER_NAME"] = request.git_author_name
    if request.git_author_email:
        env["GIT_AUTHOR_EMAIL"] = request.git_author_email
        env["GIT_COMMITTER_EMAIL"] = request.git_author_email
    return env


def _remote_name_for_url(local_path: Path, remote_url: str | None, git_runner: GitRunner) -> str:
    if remote_url:
        proc = git_runner(["remote", "-v"], cwd=local_path)
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == remote_url:
                return parts[0]
    return "origin"
