from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.scope import review_source

if TYPE_CHECKING:
    from nyanpasu.models import AgentTask, SubtaskRequest
    from nyanpasu_github_reviewer.models import GitHubReviewerConfig

SPLIT_LINE_THRESHOLD = 2000


def changed_line_count(inventory: dict) -> int:
    return sum(item["additions"] + item["deletions"] for item in inventory["files"])


def prepare_pr_split(config: GitHubReviewerConfig, parent: AgentTask, request: SubtaskRequest) -> SubtaskRequest:
    """Freeze the whole PR delta for packaging advice, independently of code admission."""
    if parent.spawned_by_task_id is not None:
        raise ValueError("PR split analysis must be requested by the root reviewer")
    if request.inputs:
        raise ValueError("pr-split inputs must be empty; put the trigger and behavior boundaries in prompt")
    inventory = parent.metadata.get("review_inventory")
    if inventory is None:
        raise ValueError("read review-scope before requesting PR split analysis")
    if request.revision not in {None, inventory["head_sha"]}:
        raise ValueError("PR split analysis must use the inventory head")
    pr = parent.metadata["pull_request"]
    source = review_source(inventory)
    instructions = Template(Path(__file__).with_name("instructions").joinpath("pr-split.md").read_text()).substitute(
        repo=pr["repo"], pr_number=pr["number"], review_language=config.review_language
    )
    assignment = {
        "source": source,
        "changed_lines": changed_line_count(inventory),
        "path_encoding": inventory.get("path_encoding", "utf-8"),
        "files": inventory["files"],
    }
    return request.model_copy(
        update={
            "kind": "github_reviewer.pr-split",
            "revision": inventory["head_sha"],
            "workspace_mode": "clone",
            "memory_enabled": False,
            "inputs": {"split_source": source},
            "developer_instructions": instructions,
            "prompt": request.prompt + "\n\nPinned PR split assignment (JSON data):\n" + json.dumps(assignment),
        }
    )


def validate_pr_split_child(parent: AgentTask, child: AgentTask) -> None:
    inventory = parent.metadata.get("review_inventory")
    if parent.spawned_by_task_id is not None or inventory is None:
        raise ValueError("PR split analysis requires a root reviewer with a pinned inventory")
    if (
        child.metadata.get("inputs", {}).get("split_source") != review_source(inventory)
        or child.workspace is None
        or child.workspace.revision != inventory["head_sha"]
    ):
        raise ValueError("stored PR split child does not match its pinned review range")
