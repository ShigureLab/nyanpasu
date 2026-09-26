from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from nyanpasu.git_ops import WorktreeManager

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import AgentTask, SubtaskRequest

INSTRUCTIONS = Path(__file__).with_name("instructions")


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    provenance: Literal["explicit", "base-contract", "inferred"]


class DesignInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


def prepare_reference(config: NyanpasuConfig, parent: AgentTask, request: SubtaskRequest) -> SubtaskRequest:
    """Pin common inputs after the reviewer decides that a reference is useful."""
    if request.purpose == "test-audit":
        return request.model_copy(
            update={
                "developer_instructions": (INSTRUCTIONS / "test-review.md").read_text(),
            }
        )
    if request.purpose != "independent-design":
        return request
    if parent.spawned_by_task_id is not None:
        raise ValueError("independent design must be requested by the root reviewer")
    inputs = DesignInputs.model_validate(request.inputs)
    workspace = parent.workspace
    if workspace is None:
        raise ValueError("independent design requires a repository")
    pr = parent.metadata["pull_request"]
    manager = WorktreeManager(config)
    manager.ensure_base_workspace(workspace)
    manager.fetch_revision(workspace)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace.local_path, check=True, text=True, capture_output=True
        ).stdout.strip()

    head = git("rev-parse", "--verify", f"{pr['head_sha']}^{{commit}}")
    remote = workspace.remote or "origin"
    # FETCH_HEAD is shared by concurrent reviews of this repository. Resolve the
    # advertised branch to an immutable identity and fetch that exact object.
    target_base = git("ls-remote", "--exit-code", remote, f"refs/heads/{pr['base_ref']}").split()[0]
    git("fetch", "--no-tags", "--no-write-fetch-head", remote, target_base)
    bases = git("merge-base", "--all", head, target_base).splitlines()
    if len(bases) != 1:
        raise ValueError("independent design requires one unambiguous merge-base")
    source = bases[0]
    policy = (INSTRUCTIONS / "independent-design.md").read_text()
    requirements = json.dumps(inputs.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    manifest = {
        "head_sha": head,
        "target_base_sha": target_base,
        "merge_base_sha": source,
        "source_tree_sha": git("rev-parse", f"{source}^{{tree}}"),
        "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
        "policy_sha256": hashlib.sha256(policy.encode()).hexdigest(),
        "isolation": "base-tree-only; filesystem and network are not isolated",
        "requirements": inputs.model_dump(mode="json"),
    }
    return request.model_copy(
        update={
            "revision": source,
            "workspace_mode": "snapshot",
            "inputs": manifest,
            "developer_instructions": policy,
            "prompt": "Design from this base snapshot and the requirements below. "
            "The original source SHA is "
            + source
            + ". The snapshot commit is a different local identity.\n"
            + requirements,
        }
    )
