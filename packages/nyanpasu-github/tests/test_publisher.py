from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from nyanpasu_github.publisher import GitHubPullRequestPublisher, PublishPullRequestRequest

if TYPE_CHECKING:
    from pathlib import Path


def test_publisher_returns_no_changes(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def git_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        _ = kwargs
        return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")

    publisher = GitHubPullRequestPublisher(git_runner=git_runner)

    result = publisher.publish(
        worktree=tmp_path,
        request=PublishPullRequestRequest(
            repo="owner/repo",
            base_branch="main",
            branch_name="nyanpasu/task",
            title="Title",
            body="Body",
            commit_message="Commit",
        ),
    )

    assert result.status == "no_changes"
    assert calls == [["status", "--porcelain"]]


def test_publisher_dry_run_does_not_commit(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def git_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        _ = kwargs
        return subprocess.CompletedProcess(["git", *args], 0, stdout=" M README.md\n", stderr="")

    publisher = GitHubPullRequestPublisher(git_runner=git_runner)

    result = publisher.publish(
        worktree=tmp_path,
        request=PublishPullRequestRequest(
            repo="owner/repo",
            base_branch="main",
            branch_name="nyanpasu/task",
            title="Title",
            body="Body",
            commit_message="Commit",
            dry_run=True,
        ),
    )

    assert result.status == "dry_run"
    assert result.changed_files == ("README.md",)
    assert calls == [["status", "--porcelain"]]


def test_publisher_create_pr_passes_labels_and_draft(tmp_path: Path) -> None:
    git_calls: list[list[str]] = []
    gh_calls: list[list[str]] = []

    def git_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        git_calls.append(args)
        _ = kwargs
        if args == ["status", "--porcelain"]:
            stdout = " M README.md\n"
        elif args == ["remote", "-v"]:
            stdout = "origin\tgit@github.com:owner/repo.git (fetch)\norigin\tgit@github.com:owner/repo.git (push)\n"
        elif args == ["rev-parse", "HEAD"]:
            stdout = "abc123\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(["git", *args], 0, stdout=stdout, stderr="")

    def gh_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        gh_calls.append(args)
        _ = kwargs
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="https://github.com/owner/repo/pull/1\n", stderr="")

    publisher = GitHubPullRequestPublisher(git_runner=git_runner, gh_runner=gh_runner)

    result = publisher.publish(
        worktree=tmp_path,
        request=PublishPullRequestRequest(
            repo="owner/repo",
            base_branch="main",
            branch_name="nyanpasu/task",
            title="Title",
            body="Body",
            commit_message="Commit",
            labels=("bot", "docs"),
            draft=True,
            remote_url="git@github.com:owner/repo.git",
        ),
    )

    assert result.status == "published"
    assert result.pr_url == "https://github.com/owner/repo/pull/1"
    assert result.pr_number == 1
    assert ["push", "--force-with-lease", "--set-upstream", "origin", "HEAD:refs/heads/nyanpasu/task"] in git_calls
    assert "--label" in gh_calls[0]
    assert gh_calls[0][-5:] == ["--label", "bot", "--label", "docs", "--draft"]


def test_publisher_existing_pr_pushes_without_creating_new_pr(tmp_path: Path) -> None:
    git_calls: list[list[str]] = []
    gh_calls: list[list[str]] = []

    def git_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        git_calls.append(args)
        _ = kwargs
        if args == ["status", "--porcelain"]:
            stdout = " M README.md\n"
        elif args == ["remote", "-v"]:
            stdout = "origin\tgit@github.com:owner/repo.git (fetch)\norigin\tgit@github.com:owner/repo.git (push)\n"
        elif args == ["rev-parse", "HEAD"]:
            stdout = "abc123\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(["git", *args], 0, stdout=stdout, stderr="")

    def gh_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        gh_calls.append(args)
        _ = kwargs
        return subprocess.CompletedProcess(["gh", *args], 0, stdout="", stderr="")

    publisher = GitHubPullRequestPublisher(git_runner=git_runner, gh_runner=gh_runner)

    result = publisher.publish(
        worktree=tmp_path,
        request=PublishPullRequestRequest(
            repo="owner/repo",
            base_branch="main",
            branch_name="nyanpasu/task",
            title="Title",
            body="Body",
            commit_message="Commit",
            existing_pr_number=7,
            existing_pr_url="https://github.com/owner/repo/pull/7",
            remote_url="git@github.com:owner/repo.git",
        ),
    )

    assert result.status == "published"
    assert result.pr_number == 7
    assert result.pr_url == "https://github.com/owner/repo/pull/7"
    assert ["push", "--force-with-lease", "--set-upstream", "origin", "HEAD:refs/heads/nyanpasu/task"] in git_calls
    assert gh_calls == []
