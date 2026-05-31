from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

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
    assert (second_context.session_worktree / "README.md").read_text(encoding="utf-8") == "hello again\n"


def test_prepare_event_snapshot_can_replace_existing_path_when_opted_in(tmp_path: Path) -> None:
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
    second = manager.prepare_event_snapshot(task)

    assert first == second
    assert second is not None
    assert (second / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_fetch_revision_uses_configured_remote_name(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _git(["init", str(repo_path)], tmp_path)
    _git(["remote", "add", "origin", "https://github.com/fork/repo.git"], repo_path)
    _git(["remote", "add", "upstream", "https://github.com/owner/repo.git"], repo_path)
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))

    workspace = WorkspaceRef(
        key="owner/repo",
        local_path=repo_path,
        remote="https://github.com/owner/repo.git",
    )

    assert manager._remote_name(workspace) == "upstream"


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
