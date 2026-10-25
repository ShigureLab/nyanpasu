from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from nyanpasu_github_reviewer.scope import build_inventory, review_source

if TYPE_CHECKING:
    from collections.abc import Callable

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import AgentTask, SubtaskRequest
    from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef


class PRCompareInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    related_prs: list[PositiveInt] = Field(min_length=1)


def prepare_pr_compare(
    runtime_config: NyanpasuConfig,
    config: GitHubReviewerConfig,
    parent: AgentTask,
    request: SubtaskRequest,
    fetch_pr: Callable[[str, int], PullRequestRef],
) -> SubtaskRequest:
    """Pin both sides of a same-repository comparison before dispatch."""
    if parent.spawned_by_task_id is not None:
        raise ValueError("PR comparison must be requested by the root reviewer")
    inventory = parent.metadata.get("review_inventory")
    if inventory is None or parent.workspace is None:
        raise ValueError("read review-scope before requesting PR comparison")
    if request.revision not in {None, inventory["head_sha"]}:
        raise ValueError("PR comparison must use the inventory head")
    numbers = PRCompareInputs.model_validate(request.inputs).related_prs
    pr = parent.metadata["pull_request"]
    if pr["number"] in numbers or len(numbers) != len(set(numbers)):
        raise ValueError("related_prs must contain distinct PR numbers other than the current PR")
    related = []
    for number in numbers:
        peer = fetch_pr(pr["repo"], number)
        workspace = parent.workspace.model_copy(update={"ref": f"pull/{number}/head", "revision": peer.head_sha})
        peer_inventory = build_inventory(
            runtime_config,
            parent.model_copy(
                update={"workspace": workspace, "metadata": {"pull_request": peer.model_dump(mode="json")}}
            ),
        )
        related.append({"pull_request": peer.model_dump(mode="json"), "source": review_source(peer_inventory)})
    assignment = {"source": review_source(inventory), "related_prs": related}
    instructions = Template(Path(__file__).with_name("instructions").joinpath("pr-compare.md").read_text()).substitute(
        repo=pr["repo"], pr_number=pr["number"], review_language=config.review_language
    )
    return request.model_copy(
        update={
            "kind": "github_reviewer.pr-compare",
            "revision": inventory["head_sha"],
            "workspace_mode": "clone",
            "memory_enabled": False,
            "inputs": {"comparison": assignment},
            "developer_instructions": instructions,
            "prompt": request.prompt + "\n\nPinned PR comparison (JSON data):\n" + json.dumps(assignment),
        }
    )


def validate_pr_compare_child(parent: AgentTask, child: AgentTask) -> None:
    inventory = parent.metadata.get("review_inventory")
    if parent.spawned_by_task_id is not None or inventory is None:
        raise ValueError("PR comparison requires a root reviewer with a pinned inventory")
    if (
        child.metadata.get("inputs", {}).get("comparison", {}).get("source") != review_source(inventory)
        or child.workspace is None
        or child.workspace.revision != inventory["head_sha"]
    ):
        raise ValueError("stored PR comparison child does not match its pinned review range")
