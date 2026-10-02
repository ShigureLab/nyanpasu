from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentTask, TaskAction, WorkspaceRef

if TYPE_CHECKING:
    from pathlib import Path


def test_prepare_context_reuses_context_key_path_and_resets_revision(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _git(["init", str(repo_path)], tmp_path)
    _git(["config", "user.email", "nyanpasu@example.invalid"], repo_path)
    _git(["config", "user.name", "Nyanpasu"], repo_path)
    (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], repo_path)
    _git(["commit", "-m", "initial"], repo_path)
    first_sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()
    (repo_path / "README.md").write_text("hello again\n", encoding="utf-8")
    _git(["commit", "-am", "second"], repo_path)
    second_sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()

    task = AgentTask(
        task_id="delivery-1",
        action=TaskAction.RUN,
        context_key="github:ExampleOrg/ExampleRepo#123",
        prompt="review",
        workspace=WorkspaceRef(
            key="ExampleOrg/ExampleRepo",
            local_path=repo_path,
            revision=first_sha,
        ),
        dedupe_key="delivery-1",
    )
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))

    first_context = manager.prepare_context(task, None)
    assert task.workspace is not None
    second_context = manager.prepare_context(
        task.model_copy(update={"workspace": task.workspace.model_copy(update={"revision": second_sha})}),
        first_context,
    )

    assert first_context.session_worktree == second_context.session_worktree
    assert second_context.session_worktree is not None
    assert (second_context.session_worktree / ".git").is_dir()
    assert (second_context.session_worktree / "README.md").read_text(encoding="utf-8") == "hello again\n"


def test_prepare_event_snapshot_resets_tracked_changes_and_removes_untracked_files(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _git(["init", str(repo_path)], tmp_path)
    _git(["config", "user.email", "nyanpasu@example.invalid"], repo_path)
    _git(["config", "user.name", "Nyanpasu"], repo_path)
    (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], repo_path)
    _git(["commit", "-m", "initial"], repo_path)
    head_sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()

    task = AgentTask(
        task_id="delivery-1",
        action=TaskAction.RUN,
        context_key="github:ExampleOrg/ExampleRepo#123",
        prompt="review",
        workspace=WorkspaceRef(
            key="ExampleOrg/ExampleRepo",
            local_path=repo_path,
            revision=head_sha,
        ),
        dedupe_key="delivery-1",
        workspace_policy="event_snapshot",
    )
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))

    first = manager.prepare_event_snapshot(task)
    assert first is not None
    (first / "README.md").write_text("modified\n", encoding="utf-8")
    (first / "untracked.txt").write_text("leftover\n", encoding="utf-8")
    second = manager.prepare_event_snapshot(task)

    assert first == second
    assert second is not None
    assert (second / ".git").is_dir()
    assert (second / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert not (second / "untracked.txt").exists()


def test_prepare_context_replaces_legacy_git_worktree_with_clone(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _git(["init", str(repo_path)], tmp_path)
    _git(["config", "user.email", "nyanpasu@example.invalid"], repo_path)
    _git(["config", "user.name", "Nyanpasu"], repo_path)
    (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], repo_path)
    _git(["commit", "-m", "initial"], repo_path)
    head_sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()

    task = AgentTask(
        task_id="delivery-1",
        action=TaskAction.RUN,
        context_key="github:ExampleOrg/ExampleRepo#123",
        prompt="review",
        workspace=WorkspaceRef(
            key="ExampleOrg/ExampleRepo",
            local_path=repo_path,
            revision=head_sha,
        ),
        dedupe_key="delivery-1",
    )
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))
    legacy_path = manager.session_worktree_path(task)
    legacy_path.parent.mkdir(parents=True)
    _git(["worktree", "add", "--detach", str(legacy_path), head_sha], repo_path)

    context = manager.prepare_context(task, None)

    assert context.session_worktree == legacy_path
    assert (legacy_path / ".git").is_dir()
    assert (legacy_path / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_fetch_revision_uses_configured_remote_name(tmp_path: Path) -> None:
    upstream_path = tmp_path / "upstream"
    _git(["init", str(upstream_path)], tmp_path)
    _git(["config", "user.email", "nyanpasu@example.invalid"], upstream_path)
    _git(["config", "user.name", "Nyanpasu"], upstream_path)
    (upstream_path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(["add", "README.md"], upstream_path)
    _git(["commit", "-m", "initial"], upstream_path)
    ref = _git(["symbolic-ref", "HEAD"], upstream_path).stdout.strip()
    fork_path = tmp_path / "fork"
    _git(["clone", str(upstream_path), str(fork_path)], tmp_path)
    repo_path = tmp_path / "repo"
    _git(["clone", str(fork_path), str(repo_path)], tmp_path)
    _git(["remote", "add", "upstream", str(upstream_path)], repo_path)
    (upstream_path / "README.md").write_text("upstream update\n", encoding="utf-8")
    _git(["commit", "-am", "upstream update"], upstream_path)
    revision = _git(["rev-parse", "HEAD"], upstream_path).stdout.strip()
    with pytest.raises(subprocess.CalledProcessError):
        _git(["cat-file", "-e", f"{revision}^{{commit}}"], repo_path)
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))

    workspace = WorkspaceRef(
        key="owner/repo",
        local_path=repo_path,
        remote=str(upstream_path),
        ref=ref,
        revision=revision,
    )

    manager.fetch_revision(workspace)

    assert _git(["rev-parse", "FETCH_HEAD"], repo_path).stdout.strip() == revision
    assert _git(["show", f"{revision}:README.md"], repo_path).stdout == "upstream update\n"


def test_remove_worktree_tolerates_stale_directory(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _git(["init", str(repo_path)], tmp_path)
    stale_path = tmp_path / "stale-worktree"
    stale_path.mkdir()
    (stale_path / "leftover").write_text("stale\n", encoding="utf-8")
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))

    manager.remove_worktree(WorkspaceRef(key="owner/repo", local_path=repo_path), stale_path)

    assert not stale_path.exists()


def _git(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], cwd=cwd, text=True, capture_output=True, check=True)
