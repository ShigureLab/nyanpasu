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


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "base"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    (repo / "file.txt").write_text("initial content")
    _git(["add", "."], repo)
    _git(["commit", "-m", "Initial"], repo)
    revision = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))
    task = AgentTask(
        task_id="first",
        action=TaskAction.RUN,
        context_key="public-pr",
        prompt="review",
        workspace=WorkspaceRef(key="repo", local_path=repo, revision=revision),
    )
    return repo, manager, task


def test_workspace_fetch_uses_base_after_clone_origin_changes(workspace):
    repo, manager, task = workspace
    context = manager.prepare_context(task, None)
    clone = context.session_worktree
    assert clone is not None
    _git(["remote", "set-url", "origin", "ssh://unreachable.invalid/repo"], clone)
    _git(["config", "core.sshCommand", "touch .git/ssh-ran; false"], clone)
    (repo / "file.txt").write_text("next revision")
    _git(["commit", "-am", "Next"], repo)
    revision = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert task.workspace is not None
    task = task.model_copy(update={"workspace": task.workspace.model_copy(update={"revision": revision})})

    updated = manager.prepare_context(task, context)

    assert updated.revision == revision
    assert (clone / "file.txt").read_text() == "next revision"
    assert not (clone / ".git" / "ssh-ran").exists()


@pytest.mark.parametrize("explicit_remote", [True, False])
def test_clone_can_checkout_revisions_from_partial_source(tmp_path, workspace, explicit_remote):
    repo, manager, task = workspace
    _git(["config", "uploadpack.allowFilter", "true"], repo)
    _git(["config", "uploadpack.allowAnySHA1InWant", "true"], repo)
    (repo / "file.txt").write_text("newer content")
    _git(["commit", "-am", "Next"], repo)
    partial = tmp_path / "partial"
    _git(["clone", "--filter=blob:none", "--no-checkout", repo.as_uri(), str(partial)], tmp_path)
    _git(["checkout", "HEAD"], partial)
    assert task.workspace is not None
    task = task.model_copy(
        update={
            "workspace": task.workspace.model_copy(
                update={"local_path": partial, "remote": repo.as_uri() if explicit_remote else None}
            )
        }
    )
    context = manager.prepare_context(task, None)
    clone = context.session_worktree
    assert clone is not None
    assert (clone / "file.txt").read_text() == "initial content"
    (repo / "file.txt").write_text("unneeded intermediate blob")
    _git(["commit", "-am", "Intermediate"], repo)
    intermediate = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "file.txt").write_text("final revision")
    _git(["commit", "-am", "Final"], repo)
    final = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    _git(["fetch", "origin"], partial)
    task = task.model_copy(update={"workspace": task.workspace.model_copy(update={"revision": final})})

    manager.prepare_context(task, context)

    assert (clone / "file.txt").read_text() == "final revision"
    assert _git(["show", f"{intermediate}:file.txt"], clone).stdout == "unneeded intermediate blob"


def _git(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], cwd=cwd, text=True, capture_output=True, check=True)
