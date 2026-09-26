from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
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
            try:
                if task.workspace_mode == "snapshot":
                    self._snapshot(workspace, session_path)
                else:
                    self._reset_worktree(workspace, session_path, workspace.revision or workspace.ref or "HEAD")
            except Exception:
                if existing is None:
                    self._remove_worktree_unlocked(workspace, session_path)
                raise
        return AgentContext(
            context_key=task.context_key,
            thread_id=existing.thread_id if existing else None,
            session_worktree=session_path,
            workspace_key=workspace.key,
            revision=workspace.revision,
        )

    def _snapshot(self, workspace: WorkspaceRef, path: Path) -> None:
        if not workspace.revision:
            raise ValueError("snapshot requires a pinned revision")
        revision = self._run(
            ["git", "rev-parse", "--verify", f"{workspace.revision}^{{commit}}"], workspace.local_path
        ).stdout.strip()
        tree = self._run(["git", "rev-parse", f"{revision}^{{tree}}"], workspace.local_path).stdout.strip()
        # Alternates do not carry a partial clone's promisor configuration.
        # Read this tree's objects through the source repo so Git can fetch missing blobs.
        objects_in_tree = self._run(
            ["git", "rev-list", "--objects", "--no-object-names", tree], workspace.local_path
        ).stdout
        subprocess.run(
            ["git", "cat-file", "--batch-check"],
            cwd=workspace.local_path,
            input=objects_in_tree,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        # Release export attributes must not omit tests or substitute source text.
        # A temporary bare repository gives info/attributes highest precedence
        # without mutating the shared repository or exposing its objects to the child.
        objects = self._run(["git", "rev-parse", "--git-path", "objects"], workspace.local_path).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="nyanpasu-export-") as directory:
            export = Path(directory)
            self._run(["git", "init", "--bare", "--template="], export)
            (export / "objects" / "info" / "alternates").write_text(
                str((workspace.local_path / objects).resolve()) + "\n"
            )
            (export / "info").mkdir(exist_ok=True)
            (export / "info" / "attributes").write_text("* -export-ignore -export-subst\n")
            archive = subprocess.run(
                ["git", "archive", "--format=tar", tree], cwd=export, capture_output=True, check=True
            ).stdout
        if path.exists():
            self._remove_worktree_unlocked(workspace, path)
        path.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
            # Git emits files, directories and symlinks. Validate member paths while
            # preserving link text, including valid targets outside the snapshot.
            contents.extractall(path, filter="tar")
        manifest = {
            "source_sha": revision,
            "source_tree_sha": tree,
            "export_sha256": hashlib.sha256(archive).hexdigest(),
            "isolation": "base-tree-only; filesystem and network are not isolated",
        }
        self._run(["git", "init", "--template="], path)
        (path / ".git" / "nyanpasu-source.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
        self._run(["git", "add", "--all", "--force"], path)
        # Archive represents gitlinks as empty directories, so restore their index entries.
        for entry in self._run(["git", "ls-tree", "-rz", revision], workspace.local_path).stdout.split("\0"):
            if entry.startswith("160000 "):
                metadata, name = entry.split("\t", 1)
                self._run(["git", "update-index", "--add", "--cacheinfo", "160000", metadata.split()[2], name], path)
        self._run(
            [
                "git",
                "-c",
                "user.name=Nyanpasu",
                "-c",
                "user.email=nyanpasu@localhost",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "--no-gpg-sign",
                "-m",
                "Reference input snapshot",
            ],
            path,
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
        if self._is_managed_clone(path):
            self._sync_clone(workspace, path, ref)
            return
        if path.exists():
            self._remove_worktree_unlocked(workspace, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clone_workspace(workspace, path)
        self._sync_clone(workspace, path, ref)

    def _is_managed_clone(self, path: Path) -> bool:
        return (path / ".git").is_dir()

    def _clone_workspace(self, workspace: WorkspaceRef, path: Path) -> None:
        self._run(["git", "clone", "--no-checkout", str(workspace.local_path), str(path)], Path.cwd())
        if workspace.remote:
            self._run(["git", "remote", "set-url", "origin", workspace.remote], path)

    def _sync_clone(self, workspace: WorkspaceRef, path: Path, ref: str) -> None:
        if workspace.remote:
            self._run(["git", "remote", "set-url", "origin", workspace.remote], path)

        target = self._fetch_clone_target(workspace, path, ref)
        self._run(["git", "checkout", "--detach", "--force", target], path)
        self._run(["git", "reset", "--hard", target], path)
        self._run(["git", "clean", "-ffd"], path)

    def _fetch_clone_target(self, workspace: WorkspaceRef, path: Path, ref: str) -> str:
        if workspace.ref is not None:
            try:
                self._run(["git", "fetch", "--force", "origin", workspace.ref], path)
            except subprocess.CalledProcessError:
                if workspace.revision is None:
                    raise
        if workspace.revision is not None:
            try:
                self._run(["git", "cat-file", "-e", f"{workspace.revision}^{{commit}}"], path)
            except subprocess.CalledProcessError:
                self._run(["git", "fetch", "--force", "origin", workspace.revision], path)
            return workspace.revision
        if workspace.ref is not None:
            return "FETCH_HEAD"
        return ref

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
