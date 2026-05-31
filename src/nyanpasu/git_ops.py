from __future__ import annotations

import re
import shutil
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from nyanpasu.models import AgentContext, AgentTask, WorkspaceRef

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig

SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_slug(value: str) -> str:
    slug = SAFE_PATH_RE.sub("-", value).strip("-")
    return slug or "unknown"


class WorktreeManager:
    def __init__(self, config: NyanpasuConfig) -> None:
        self.config = config
        self._locks_guard = threading.Lock()
        self._workspace_locks: dict[str, threading.Lock] = {}

    def prepare_context(self, task: AgentTask, existing: AgentContext | None) -> AgentContext:
        if task.workspace is None:
            return AgentContext(
                context_key=task.context_key,
                thread_id=existing.thread_id if existing else None,
                session_worktree=None,
                workspace_key=None,
                revision=None,
            )
        workspace = task.workspace
        self.ensure_base_workspace(workspace)
        with self._workspace_lock(workspace.key):
            self.fetch_revision(workspace)
            session_path = self.session_worktree_path(task)
            self._reset_worktree(workspace, session_path, workspace.revision or workspace.ref or "HEAD")
        return AgentContext(
            context_key=task.context_key,
            thread_id=existing.thread_id if existing else None,
            session_worktree=session_path,
            workspace_key=workspace.key,
            revision=workspace.revision,
        )

    def prepare_event_snapshot(self, task: AgentTask) -> Path | None:
        if task.workspace is None:
            return None
        workspace = task.workspace
        self.ensure_base_workspace(workspace)
        with self._workspace_lock(workspace.key):
            self.fetch_revision(workspace)
            path = self.event_snapshot_path(task)
            self._reset_worktree(workspace, path, workspace.revision or workspace.ref or "HEAD")
            return path

    def ensure_base_workspace(self, workspace: WorkspaceRef) -> None:
        with self._workspace_lock(workspace.key):
            if (workspace.local_path / ".git").exists():
                return
            if workspace.remote is None:
                raise ValueError(f"workspace is missing and remote is not configured: {workspace.local_path}")
            workspace.local_path.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                ["git", "clone", "--filter=blob:none", "--no-checkout", workspace.remote, str(workspace.local_path)],
                Path.cwd(),
            )

    def fetch_revision(self, workspace: WorkspaceRef) -> None:
        if workspace.ref is None and workspace.revision is None:
            return
        if workspace.ref is not None:
            remote = self._remote_name(workspace)
            try:
                self._run(["git", "fetch", "--force", remote, workspace.ref], workspace.local_path)
                return
            except subprocess.CalledProcessError:
                if workspace.revision is None:
                    raise
        if workspace.revision is not None:
            self._run(["git", "cat-file", "-e", f"{workspace.revision}^{{commit}}"], workspace.local_path)

    def remove_worktree(self, workspace: WorkspaceRef | None, path: Path | None) -> None:
        if workspace is None or path is None or not path.exists():
            return
        with self._workspace_lock(workspace.key):
            self._remove_worktree_unlocked(workspace, path)

    def session_worktree_path(self, task: AgentTask) -> Path:
        return self.config.worktrees_dir / safe_slug(task.context_key)

    def event_snapshot_path(self, task: AgentTask) -> Path:
        revision = task.workspace.revision if task.workspace else ""
        suffix = f"-{revision[:12]}" if revision else ""
        return (
            self.config.worktrees_dir / "_events" / safe_slug(task.context_key) / f"{safe_slug(task.task_id)}{suffix}"
        )

    def event_worktree_path(self, task: AgentTask) -> Path:
        return self.event_snapshot_path(task)

    def _reset_worktree(self, workspace: WorkspaceRef, path: Path, ref: str) -> None:
        if path.exists():
            self._remove_worktree_unlocked(workspace, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["git", "worktree", "add", "--detach", str(path), ref], workspace.local_path)

    def _remove_worktree_unlocked(self, workspace: WorkspaceRef, path: Path) -> None:
        with suppress(subprocess.CalledProcessError):
            self._run(["git", "worktree", "remove", "--force", str(path)], workspace.local_path)
        if path.exists():
            shutil.rmtree(path)
        with suppress(subprocess.CalledProcessError):
            self._run(["git", "worktree", "prune"], workspace.local_path)

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=True)

    def _workspace_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._workspace_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._workspace_locks[key] = lock
            return lock

    def _remote_name(self, workspace: WorkspaceRef) -> str:
        if workspace.remote:
            remotes = self._run(["git", "remote", "-v"], workspace.local_path).stdout.splitlines()
            for line in remotes:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == workspace.remote:
                    return parts[0]
        return "origin"
