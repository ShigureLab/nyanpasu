from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nyanpasu.models import WorkspaceRef
    from nyanpasu_github_reviewer.models import PullRequestRef


def is_base_refresh(workspace: WorkspaceRef, pr: PullRequestRef, source: dict) -> bool:
    old_head, old_base = source.get("head_sha"), source.get("merge_base_sha")
    head, base = pr.head_sha, pr.base_sha
    if (
        source.get("base_ref") != pr.base_ref
        or not source.get("inventory_id")
        or not isinstance(old_head, str)
        or not isinstance(old_base, str)
        or old_base == base
        or not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (old_head, old_base, head, base))
    ):
        return False

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=workspace.local_path, capture_output=True, check=check, timeout=30)

    try:
        missing = [
            sha
            for sha in dict.fromkeys((old_head, old_base, head, base))
            if git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode
        ]
        if missing:
            git("fetch", "--no-tags", "--no-write-fetch-head", workspace.remote or "origin", *missing)
        merge_bases = git("merge-base", "--all", head, base).stdout.decode().splitlines()
        if head == old_head:
            return merge_bases == [old_base]
        if len(merge_bases) != 1 or merge_bases[0] == old_base:
            return False
        base = merge_bases[0]
        if git("merge-base", "--is-ancestor", old_base, base, check=False).returncode:
            return False

        diff = (
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "--no-relative",
        )
        if git(*diff, old_base, old_head, "--").stdout == git(*diff, base, head, "--").stdout:
            return True

        if git("merge-base", "--is-ancestor", old_head, head, check=False).returncode:
            # A clean rebase can change the same blobs on both sides of the
            # diff. Reapply the old changes to the new base and compare trees.
            return (
                git("merge-tree", "--write-tree", old_head, base).stdout == git("rev-parse", f"{head}^{{tree}}").stdout
            )

        # A poll can observe multiple merges at once. Prove every first-parent
        # step and require the last merge to have incorporated the current base.
        chain = git("rev-list", "--first-parent", "--reverse", f"{old_head}..{head}").stdout.decode().splitlines()
        for commit in chain:
            parents = git("show", "-s", "--format=%P", commit).stdout.decode().split()
            if len(parents) != 2 or parents[0] != old_head:
                return False
            incoming = parents[1]
            if git("merge-base", "--is-ancestor", old_base, incoming, check=False).returncode:
                return False
            if git("merge-base", "--is-ancestor", incoming, base, check=False).returncode:
                return False
            merged_tree = git("merge-tree", "--write-tree", old_head, incoming).stdout.strip()
            if merged_tree != git("rev-parse", f"{commit}^{{tree}}").stdout.strip():
                return False
            old_head, old_base = commit, incoming
        return old_base == base
    except (OSError, subprocess.SubprocessError):
        # Missing objects, conflicts and older Git versions all leave the event eligible.
        return False
