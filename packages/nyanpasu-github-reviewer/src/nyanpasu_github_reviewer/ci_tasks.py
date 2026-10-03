from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nyanpasu.targets import ExecutionOverride
from nyanpasu_github_reviewer.ci import CISnapshot
from nyanpasu_github_reviewer.models import PullRequestRef
from nyanpasu_github_reviewer.prompt import build_ci_analysis_instructions, build_ci_analysis_prompt

if TYPE_CHECKING:
    from nyanpasu.models import AgentTask, SubtaskRequest
    from nyanpasu_github_reviewer.models import GitHubReviewerConfig


def ci_snapshot_data(snapshot: CISnapshot) -> dict[str, Any]:
    return {**snapshot.model_dump(mode="json"), "fingerprint": snapshot.fingerprint}


def prepare_ci_analysis(
    config: GitHubReviewerConfig,
    parent: AgentTask,
    request: SubtaskRequest,
    pr: PullRequestRef,
    snapshot: CISnapshot,
) -> SubtaskRequest:
    """Freeze the failed executions, independently of changed-file review scope."""
    if parent.spawned_by_task_id is not None:
        raise ValueError("CI analysis must be requested by the root reviewer")
    pinned_pr = PullRequestRef.model_validate(parent.metadata["pull_request"])
    if pr.head_sha != pinned_pr.head_sha:
        raise ValueError("PR head changed; do not analyze the new head from this review run")
    if not snapshot.failures:
        raise ValueError("CI has no current failures; remove the CI dashboard section instead")
    if request.inputs != {"fingerprint": snapshot.fingerprint}:
        raise ValueError("CI changed; call ci-refresh and submit its fingerprint with a new request key")
    if request.revision not in {None, snapshot.head_sha}:
        raise ValueError("CI analysis must use the observed PR head")
    data = ci_snapshot_data(snapshot)
    return request.model_copy(
        update={
            "kind": "github_reviewer.ci-analysis",
            "execution": ExecutionOverride(),
            "revision": snapshot.head_sha,
            "workspace_mode": "clone",
            "memory_enabled": False,
            "inputs": {"ci_snapshot": data},
            "developer_instructions": build_ci_analysis_instructions(config, pr),
            "prompt": build_ci_analysis_prompt(pr, snapshot=data),
        }
    )


def validate_ci_child(parent: AgentTask, child: AgentTask) -> None:
    """Validate frozen CI inputs at restart without requiring a deep-review plan."""
    if parent.spawned_by_task_id is not None:
        raise ValueError("CI analysis requires a root reviewer")
    data = child.metadata.get("inputs", {}).get("ci_snapshot")
    if not isinstance(data, dict):
        raise ValueError("stored CI child has no execution snapshot")
    snapshot = CISnapshot.model_validate({key: value for key, value in data.items() if key != "fingerprint"})
    pr = PullRequestRef.model_validate(parent.metadata["pull_request"])
    if not snapshot.failures or data.get("fingerprint") != snapshot.fingerprint:
        raise ValueError("stored CI child has an invalid execution snapshot")
    if snapshot.head_sha != pr.head_sha or child.workspace is None or child.workspace.revision != snapshot.head_sha:
        raise ValueError("stored CI child revision does not match its PR head")
