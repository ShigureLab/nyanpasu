from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote_from_bytes

from pydantic import BaseModel, ConfigDict, Field

from nyanpasu.git_ops import WorktreeManager

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import AgentTask


class ScopeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[str] = Field(min_length=1, description="Exact changed-path tokens from the inventory, never globs.")
    category: Literal["production", "tests", "examples", "evidence", "generated", "vendor", "other"]
    decision: Literal["accept", "relocate", "clarify"]
    reason: str = Field(pattern=r"\S", description="Concrete rationale for the admission decision.")
    source: str = Field(pattern=r"\S", description="Original requirement or existing caller evidence.")
    alternative: str = Field(pattern=r"\S", description="Alternative placement or simpler way to satisfy the contract.")


class ScopePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_id: str = Field(description="ID from the current pinned inventory.")
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
    target = pr.get("base_sha") or git("ls-remote", "--exit-code", remote, f"refs/heads/{pr['base_ref']}").split()[0]
    git("fetch", "--no-tags", "--no-write-fetch-head", remote, target)
    bases = git("merge-base", "--all", head, target).splitlines()
    if len(bases) != 1:
        raise ValueError("review scope requires one unambiguous merge-base")
    base = bases[0]
    inventory = {
        "head_sha": head,
        "base_ref": pr["base_ref"],
        "target_base_sha": target,
        "merge_base_sha": base,
        "source_tree_sha": git("rev-parse", f"{base}^{{tree}}").strip(),
        **_range_stats(workspace.local_path, base, head),
    }
    return {"inventory_id": _inventory_id(inventory), **inventory}


def _range_stats(repo: Path, base: str, head: str) -> dict:
    # Treat a rename as removal + addition: both paths remain accountable, including
    # binary files and paths containing tabs/newlines. No GitHub API file-count cap.
    numstat = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base, head, "--"],
        cwd=repo,
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
    return {"path_encoding": path_encoding, "files": files, "directories": directories}


def _inventory_id(inventory: dict) -> str:
    # Advancing the target without changing the merge-base leaves this diff intact.
    identity = {key: value for key, value in inventory.items() if key not in {"inventory_id", "target_base_sha"}}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def review_source(inventory: dict) -> dict:
    return {key: inventory[key] for key in ("head_sha", "base_ref", "merge_base_sha", "inventory_id")}


def build_review_delta(repo: Path, inventory: dict, prior_review: dict) -> dict | None:
    """Compare against a completed dashboard review whose pinned range still verifies."""
    if prior_review.get("status") not in {"approved", "changes_requested", "comment"}:
        return None
    source = prior_review.get("source")
    if not isinstance(source, dict):
        return None
    base = inventory["merge_base_sha"]
    previous_head = source.get("head_sha")
    if (
        source.get("base_ref") != inventory["base_ref"]
        or source.get("merge_base_sha") != base
        or not isinstance(previous_head, str)
        or not re.fullmatch(r"[0-9a-f]{40}", previous_head)
    ):
        return None
    try:
        previous = {**inventory, "head_sha": previous_head, **_range_stats(repo, base, f"{previous_head}^{{commit}}")}
        previous_id = _inventory_id(previous)
        if previous_id != source.get("inventory_id"):
            return None
        delta = _range_stats(repo, previous_head, inventory["head_sha"])
    except subprocess.CalledProcessError:
        # A force-pushed or pruned commit may no longer be available locally.
        return None
    return {
        "previous_source": review_source({**previous, "inventory_id": previous_id}),
        "path_encoding": delta["path_encoding"],
        "files": delta["files"],
        "open_findings": [
            {"id": finding_id, **{key: finding[key] for key in ("title", "status", "url")}}
            for finding_id, finding in prior_review.get("findings", {}).items()
            if finding["status"] in {"unresolved", "partial"}
        ],
    }


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
        inventory["inventory_id"]: {
            "inventory_id": plan.inventory_id,
            "path_encoding": inventory.get("path_encoding", "utf-8"),
            "counts": dict(counts),
            "groups": [group.model_dump() for group in plan.groups],
        }
    }
