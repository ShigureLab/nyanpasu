from __future__ import annotations

import hashlib
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


def context_directory(value: str) -> str:
    return f"{safe_slug(value)[:64]}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


class WorktreeManager:
    def __init__(self, config: NyanpasuConfig) -> None:
        self.config = config
        self._locks_guard = threading.Lock()
        self._workspace_locks: dict[str, threading.Lock] = {}

    def prepare_context(self, task: AgentTask, existing: AgentContext | None) -> AgentContext:
        if task.workspace is None:
            path = self.config.state_dir / "scratch" / context_directory(task.context_key)
            path.mkdir(parents=True, exist_ok=True)
            return AgentContext(
                context_key=task.context_key,
                thread_id=existing.thread_id if existing else None,
                session_worktree=path,
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
        tree, objects_in_tree = self._materialize_tree(workspace, revision)
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
            archive = export / "source.tar"
            self._run(["git", "archive", "--format=tar", f"--output={archive}", tree], export)
            with archive.open("rb") as stream:
                export_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            if path.exists():
                self._remove_worktree_unlocked(workspace, path)
            path.mkdir(parents=True)
            with tarfile.open(archive) as contents:
                # Git emits files, directories and symlinks. Validate member paths while
                # preserving link text, including valid targets outside the snapshot.
                contents.extractall(path, filter="tar")
        manifest = {
            "source_sha": revision,
            "source_tree_sha": tree,
            "export_sha256": export_sha256,
            "isolation": "base-tree-only; filesystem and network are not isolated",
        }
        self._run(["git", "init", "--template="], path)
        (path / ".git" / "nyanpasu-source.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
        # Keep raw committed files and experimental diffs independent of clean filters.
        (path / ".git" / "info").mkdir()
        (path / ".git" / "info" / "attributes").write_text("* -text -filter -working-tree-encoding -ident\n")
        # Import only this tree's objects, never source commits or author history.
        # Loading the index directly preserves blobs, modes and gitlinks without
        # applying .gitattributes clean filters to the archived working files.
        self._import_objects(workspace, path, objects_in_tree)
        self._run(["git", "read-tree", tree], path)
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
        if path is None or not path.exists():
            return
        if workspace is None:
            if path.resolve().parent == (self.config.state_dir / "scratch").resolve():
                shutil.rmtree(path)
            return
        with self._workspace_lock(workspace.key):
            self._remove_worktree_unlocked(workspace, path)

    def session_worktree_path(self, task: AgentTask) -> Path:
        return self.config.worktrees_dir / context_directory(task.context_key)

    def event_snapshot_path(self, task: AgentTask) -> Path:
        revision = task.workspace.revision if task.workspace else ""
        suffix = f"-{revision[:12]}" if revision else ""
        return (
            self.config.worktrees_dir
            / "_events"
            / context_directory(task.context_key)
            / f"{context_directory(task.task_id)}{suffix}"
        )

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
        remote = workspace.remote
        if remote is None:
            try:
                remote = self._run(["git", "config", "--get", "remote.origin.url"], workspace.local_path).stdout.strip()
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 1:  # A local-only base may have no origin.
                    raise
        if remote:
            self._run(["git", "remote", "set-url", "origin", remote], path)

        target = self._fetch_clone_target(workspace, path, ref, partial=bool(remote))
        self._run(["git", "checkout", "--detach", "--force", target], path)
        self._run(["git", "reset", "--hard", target], path)
        self._run(["git", "clean", "-ffd"], path)

    def _fetch_clone_target(self, workspace: WorkspaceRef, path: Path, ref: str, *, partial: bool) -> str:
        # The base already fetched the requested ref. Sync from its pinned commit
        # even when an earlier task changed the session clone's origin.
        source = workspace.revision or ("FETCH_HEAD" if workspace.ref else ref)
        target = self._run(
            ["git", "rev-parse", "--verify", f"{source}^{{commit}}"], workspace.local_path
        ).stdout.strip()
        _, objects_in_tree = self._materialize_tree(workspace, target)
        # A network-backed clone can lazily fetch history from its actual origin.
        # A local-only base has no such provider, so transfer its complete history.
        # Import the current tree's blobs explicitly because negotiation may omit
        # objects missing from a previously copied partial clone.
        source_url = workspace.local_path.as_uri()
        self._run(
            [
                "git",
                "fetch",
                "--upload-pack=git -c uploadpack.allowFilter=true upload-pack",
                *(["--filter=blob:none"] if partial else []),
                "--force",
                "--no-tags",
                "--no-recurse-submodules",
                source_url,
                target,
            ],
            path,
        )
        if partial:
            # Git persists filtered fetch URLs as promisor remotes. Keep history
            # fetches on the original upstream, which has the missing blobs.
            self._run(["git", "config", "--remove-section", f"remote.{source_url}"], path)
            self._run(["git", "config", "remote.origin.promisor", "true"], path)
        self._import_objects(workspace, path, objects_in_tree)
        return target

    def _materialize_tree(self, workspace: WorkspaceRef, revision: str) -> tuple[str, str]:
        tree = self._run(["git", "rev-parse", f"{revision}^{{tree}}"], workspace.local_path).stdout.strip()
        # Fetch missing blobs through the original partial clone and its origin.
        objects = self._run(["git", "rev-list", "--objects", "--no-object-names", tree], workspace.local_path).stdout
        subprocess.run(
            ["git", "cat-file", "--batch-check"],
            cwd=workspace.local_path,
            input=objects,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        return tree, objects

    def _import_objects(self, workspace: WorkspaceRef, path: Path, objects: str) -> None:
        with tempfile.TemporaryFile() as pack:
            subprocess.run(
                ["git", "pack-objects", "--stdout"],
                cwd=workspace.local_path,
                input=objects.encode("ascii"),
                stdout=pack,
                stderr=subprocess.PIPE,
                check=True,
            )
            pack.seek(0)
            subprocess.run(["git", "unpack-objects"], cwd=path, stdin=pack, capture_output=True, check=True)

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
