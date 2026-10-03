from __future__ import annotations

import os
import shutil
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentTask, TaskAction, WorkspaceRef


@pytest.fixture(autouse=True)
def require_git_isolation(tmp_path):
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed on this test host")
    workspace = tmp_path / "probe"
    workspace.mkdir()
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))
    try:
        manager._run_agent(["true"], workspace)
    except subprocess.CalledProcessError as exc:
        if any(
            error in exc.stderr
            for error in (
                "No permissions to create a new namespace",
                "Creating new namespace failed",
                "Can't mount proc",
            )
        ):
            pytest.skip(f"test host does not permit Git isolation: {exc.stderr.strip()}")
        raise


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _repository(tmp_path):
    repo = tmp_path / "base"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("initial content")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial")
    revision = _git(repo, "rev-parse", "HEAD")
    manager = WorktreeManager(NyanpasuConfig(state_dir=tmp_path / "state"))
    task = AgentTask(
        task_id="first",
        action=TaskAction.RUN,
        context_key="public-pr",
        prompt="review",
        workspace=WorkspaceRef(key="repo", local_path=repo, revision=revision),
    )
    return repo, manager, task


def test_workspace_hook_runs_without_host_files_native_history_or_service_secrets(tmp_path, monkeypatch):
    repo, manager, task = _repository(tmp_path)
    context = manager.prepare_context(task, None)
    clone = context.session_worktree
    assert clone is not None
    private = tmp_path / "private-memory.md"
    private.write_text("private audience fact")
    native = tmp_path / "operator" / ".codex"
    native.mkdir(parents=True)
    (native / "history.jsonl").write_text("another audience's native history")
    monkeypatch.setenv("NYANPASU_TOKEN", "service-admin-token")
    monkeypatch.setenv("CODEX_HOME", str(native))
    hook = clone / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        f"cat '{private}' > .git/leaked 2>/dev/null\n"
        f"cat '/proc/{os.getpid()}/root{private}' >> .git/leaked 2>/dev/null\n"
        f"cat '{native / 'history.jsonl'}' >> .git/leaked 2>/dev/null\n"
        f"test -e '{repo}' && echo base-visible >> .git/leaked\n"
        'printf "%s" "${NYANPASU_TOKEN-unset}" > .git/observed-token\n'
        'printf "%s" "$HOME" > .git/observed-home\n'
        "echo executed > .git/hook-marker\n"
    )
    hook.chmod(0o755)

    manager.prepare_context(task, context)

    assert (clone / ".git" / "hook-marker").read_text().strip() == "executed"
    assert (clone / ".git" / "leaked").read_text() == ""
    assert (clone / ".git" / "observed-token").read_text() == "unset"
    assert not Path((clone / ".git" / "observed-home").read_text()).exists()
    assert private.read_text() == "private audience fact"


def test_local_clone_objects_do_not_share_writable_inodes_with_base(tmp_path):
    repo, manager, task = _repository(tmp_path)
    clone = manager.prepare_context(task, None).session_worktree
    assert clone is not None
    blob = _git(repo, "rev-parse", "HEAD:file.txt")
    relative = Path(".git") / "objects" / blob[:2] / blob[2:]
    source_object = repo / relative
    cloned_object = clone / relative
    assert source_object.stat().st_ino != cloned_object.stat().st_ino
    source_contents = source_object.read_bytes()
    cloned_object.chmod(0o600)
    cloned_object.write_bytes(b"agent corruption")
    assert source_object.read_bytes() == source_contents
    assert _git(repo, "show", "HEAD:file.txt") == "initial content"


def test_workspace_fetch_uses_trusted_base_after_remote_has_moved(tmp_path):
    repo, manager, task = _repository(tmp_path)
    context = manager.prepare_context(task, None)
    clone = context.session_worktree
    assert clone is not None
    # An agent may replace origin and its SSH helper. Service reconciliation
    # still gets the requested commit from its trusted base without invoking it.
    _git(clone, "remote", "set-url", "origin", "ssh://unreachable.invalid/repo")
    _git(clone, "config", "core.sshCommand", "touch .git/ssh-ran; false")
    (repo / "file.txt").write_text("next revision")
    _git(repo, "commit", "-am", "Next")
    revision = _git(repo, "rev-parse", "HEAD")
    assert task.workspace is not None
    task = task.model_copy(update={"workspace": task.workspace.model_copy(update={"revision": revision})})

    updated = manager.prepare_context(task, context)

    assert updated.revision == revision
    assert (clone / "file.txt").read_text() == "next revision"
    assert not (clone / ".git" / "ssh-ran").exists()


def test_agent_clone_can_checkout_a_base_revision_from_partial_source(tmp_path):
    repo, manager, task = _repository(tmp_path)
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    (repo / "file.txt").write_text("newer content")
    _git(repo, "commit", "-am", "Next")
    partial = tmp_path / "partial"
    _git(tmp_path, "clone", "--filter=blob:none", "--no-checkout", repo.as_uri(), str(partial))
    _git(partial, "checkout", "HEAD")
    assert task.workspace is not None
    task = task.model_copy(update={"workspace": task.workspace.model_copy(update={"local_path": partial})})
    clone = manager.prepare_context(task, None).session_worktree
    assert clone is not None
    assert (clone / "file.txt").read_text() == "initial content"
    (repo / "file.txt").write_text("unneeded intermediate blob")
    _git(repo, "commit", "-am", "Intermediate")
    (repo / "file.txt").write_text("final revision")
    _git(repo, "commit", "-am", "Final")
    final = _git(repo, "rev-parse", "HEAD")
    _git(partial, "fetch", "origin")
    task = task.model_copy(update={"workspace": task.workspace.model_copy(update={"revision": final})})
    try:
        manager.prepare_context(task, None)
    except subprocess.CalledProcessError as exc:
        pytest.fail(exc.stderr)
    assert (clone / "file.txt").read_text() == "final revision"


@contextmanager
def _git_daemon(root):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server = subprocess.Popen(
        [
            "git",
            "daemon",
            "--verbose",
            "--export-all",
            "--listen=127.0.0.1",
            f"--port={port}",
            f"--base-path={root}",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stderr is not None
        assert "Ready to rumble" in server.stderr.readline()
        yield f"git://127.0.0.1:{port}"
    finally:
        server.terminate()
        server.communicate(timeout=5)


@pytest.mark.parametrize("explicit_remote", [True, False])
def test_isolated_history_lazily_fetches_from_trusted_origin_without_base_mount(tmp_path, explicit_remote):
    repo, manager, task = _repository(tmp_path)
    historical = _git(repo, "rev-parse", "HEAD")
    historical_blob = _git(repo, "rev-parse", "HEAD:file.txt")
    (repo / "file.txt").write_text("current content")
    _git(repo, "commit", "-am", "Current")
    current = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    with _git_daemon(tmp_path) as server:
        remote = f"{server}/base"
        partial = tmp_path / "partial"
        _git(tmp_path, "clone", "--filter=blob:none", "--no-checkout", remote, str(partial))
        task = task.model_copy(
            update={
                "workspace": WorkspaceRef(
                    key="repo", local_path=partial, remote=remote if explicit_remote else None, revision=current
                )
            }
        )
        clone = manager.prepare_context(task, None).session_worktree
        assert clone is not None
        missing = _git(clone, "rev-list", "--objects", "--missing=print", "HEAD")
        assert f"?{historical_blob}" in missing
        manager._run_agent(["test", "!", "-e", str(partial)], clone)

        history = manager._run_agent(["git", "show", f"{historical}:file.txt"], clone)

        assert history.stdout == "initial content"
        assert _git(clone, "remote") == "origin"
        assert _git(clone, "remote", "get-url", "origin") == remote
        assert _git(clone, "config", "remote.origin.promisor") == "true"


def test_local_only_source_transfers_history_without_a_promisor_provider(tmp_path):
    repo, manager, task = _repository(tmp_path)
    context = manager.prepare_context(task, None)
    clone = context.session_worktree
    assert clone is not None
    (repo / "file.txt").write_text("intermediate history")
    _git(repo, "commit", "-am", "Intermediate")
    intermediate = _git(repo, "rev-parse", "HEAD")
    (repo / "file.txt").write_text("current content")
    _git(repo, "commit", "-am", "Current")
    current = _git(repo, "rev-parse", "HEAD")
    assert task.workspace is not None
    task = task.model_copy(update={"workspace": task.workspace.model_copy(update={"revision": current})})

    manager.prepare_context(task, context)

    manager._run_agent(["test", "!", "-e", str(repo)], clone)
    history = manager._run_agent(["git", "show", f"{intermediate}:file.txt"], clone)
    assert history.stdout == "intermediate history"
    assert _git(clone, "remote") == "origin"
