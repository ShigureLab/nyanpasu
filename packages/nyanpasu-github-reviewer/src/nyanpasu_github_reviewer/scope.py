from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote_from_bytes

from pydantic import BaseModel, ConfigDict, Field

from nyanpasu.git_ops import WorktreeManager

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import AgentTask


class ScopeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[str] = Field(min_length=1)
    category: Literal["production", "tests", "examples", "evidence", "generated", "vendor", "other"]
    decision: Literal["accept", "relocate", "clarify"]
    reason: str = Field(pattern=r"\S")
    source: str = Field(pattern=r"\S")
    alternative: str = Field(pattern=r"\S")


class ScopePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_id: str
    groups: list[ScopeGroup]


def build_inventory(config: NyanpasuConfig, task: AgentTask) -> dict:
    """Read only Git metadata and numstat; never feed the full patch to triage."""
    workspace = task.workspace
    if workspace is None:
        raise ValueError("review scope requires a repository")
    manager = WorktreeManager(config)
    manager.ensure_base_workspace(workspace)
    manager.fetch_revision(workspace)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace.local_path, capture_output=True, text=True, check=True
        ).stdout

    pr = task.metadata["pull_request"]
    head = git("rev-parse", "--verify", f"{pr['head_sha']}^{{commit}}").strip()
    remote = workspace.remote or "origin"
    target = git("ls-remote", "--exit-code", remote, f"refs/heads/{pr['base_ref']}").split()[0]
    git("fetch", "--no-tags", "--no-write-fetch-head", remote, target)
    bases = git("merge-base", "--all", head, target).splitlines()
    if len(bases) != 1:
        raise ValueError("review scope requires one unambiguous merge-base")
    base = bases[0]
    # Treat a rename as removal + addition: both paths remain accountable, including
    # binary files and paths containing tabs/newlines. No GitHub API file-count cap.
    numstat = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base, head, "--"],
        cwd=workspace.local_path,
        capture_output=True,
        check=True,
    ).stdout
    records = [record.split(b"\t", 2) for record in numstat.split(b"\0") if record]
    try:
        paths = [record[2].decode("utf-8") for record in records]
        path_encoding = "utf-8"
    except UnicodeDecodeError:
        # Encode every path to avoid collisions with literal percent sequences.
        paths = [quote_from_bytes(record[2], safe="/") for record in records]
        path_encoding = "percent"
    files = []
    directories: dict[str, dict[str, int]] = {}
    for (added, deleted, _), path in zip(records, paths, strict=True):
        binary = added == b"-"
        additions, deletions = (0, 0) if binary else (int(added), int(deleted))
        files.append({"path": path, "additions": additions, "deletions": deletions, "binary": binary})
        directory = path.split("/")[0] if "/" in path else "(root)"
        counts = directories.setdefault(directory, {"files": 0, "additions": 0, "deletions": 0})
        counts["files"] += 1
        counts["additions"] += additions
        counts["deletions"] += deletions
    inventory = {
        "head_sha": head,
        "target_base_sha": target,
        "merge_base_sha": base,
        "source_tree_sha": git("rev-parse", f"{base}^{{tree}}").strip(),
        "path_encoding": path_encoding,
        "files": files,
        "directories": directories,
    }
    digest = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    return {"inventory_id": digest, **inventory}


def validate_plan(inventory: dict, payload: dict) -> ScopePlan:
    plan = ScopePlan.model_validate(payload)
    if plan.inventory_id != inventory["inventory_id"]:
        raise ValueError("scope plan belongs to another inventory; inspect review-scope again")
    counts = Counter(path for group in plan.groups for path in group.files)
    expected = {item["path"] for item in inventory["files"]}
    missing, unknown = sorted(expected - counts.keys()), sorted(counts.keys() - expected)
    duplicates = sorted(path for path, count in counts.items() if count != 1)
    if missing or unknown or duplicates:
        raise ValueError(
            f"scope must cover each changed file once: missing={missing}, unknown={unknown}, duplicate={duplicates}"
        )
    return plan


def admitted_files(plan: ScopePlan) -> set[str]:
    return {path for group in plan.groups if group.decision == "accept" for path in group.files}


def scope_report(inventory: dict, plan: ScopePlan) -> dict:
    counts = Counter(dict.fromkeys(("accept", "relocate", "clarify"), 0))
    for group in plan.groups:
        counts[group.decision] += len(group.files)
    return {
        inventory["head_sha"]: {
            "inventory_id": plan.inventory_id,
            "path_encoding": inventory.get("path_encoding", "utf-8"),
            "counts": dict(counts),
            "groups": [group.model_dump() for group in plan.groups],
        }
    }
