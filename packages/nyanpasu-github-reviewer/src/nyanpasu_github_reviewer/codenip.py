from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nyanpasu.targets import ExecutionOverride

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import AgentTask, SubtaskRequest

CODENIP_PURPOSES = {"codenip", "codenip-rule"}


def prepare_codenip(
    config: NyanpasuConfig, skill: Path | None, parent: AgentTask, request: SubtaskRequest
) -> SubtaskRequest:
    if skill is None:
        raise ValueError("configure codenip_skill before dispatching a Codenip review")
    kind = f"github_reviewer.{request.purpose}"
    policy = config.tasks.kinds.get(kind)
    if policy is None or policy.execution.model is None:
        raise ValueError(f"configure an explicit model for tasks.kinds.{kind}.execution")

    inputs = {}
    if request.purpose == "codenip":
        if parent.spawned_by_task_id is not None:
            raise ValueError("Codenip coordination must be requested by the root reviewer")
        instructions = (
            f"Coordinate the experimental audit under the installed Codenip skill at {skill}. "
            "Select rules that address concrete questions in your pinned scope. Dispatch one fresh managed "
            "`purpose: codenip-rule` child per selected rule and scope. Set `inputs.rule` to the relative rule page "
            "from the index and `inputs.review_files` to the assigned paths. Omit `kind`, `execution` and "
            "`developer_instructions` to use the configured lightweight route and role. Include the exact "
            "comparison range and task evidence required by that rule in `prompt`. Use task-control `await` for "
            "the results, reconcile them against the pinned code and retain rule IDs and gaps. Mark each supported "
            "suggestion `[experimental · RULE_ID]` with its actual ID. Freeze the aggregate with task-control "
            "`complete`, then end the turn. The root reviewer owns publication."
        )
    else:
        if parent.metadata.get("purpose") != "codenip":
            raise ValueError("Codenip rule workers must be requested by the Codenip coordinator")
        rule_id, instructions = _rule_instructions(skill, request.inputs.get("rule"))
        inputs = {"rule_id": rule_id}

    return request.model_copy(
        update={
            "kind": kind,
            "execution": ExecutionOverride(),
            "workspace_mode": "worktree",
            "memory_enabled": False,
            "inputs": inputs,
            "developer_instructions": instructions,
        }
    )


def _rule_instructions(skill: Path, page: object) -> tuple[str, str]:
    if not isinstance(page, str) or not re.fullmatch(r"rules/[a-z0-9-]+/rule\.md", page):
        raise ValueError("inputs.rule must be one relative rule page from the installed Codenip index")
    rules_dir = (skill.parent / "rules").resolve()
    rule_path = (skill.parent / page).resolve()
    if not rule_path.is_relative_to(rules_dir):
        raise ValueError("the rule page must be inside the installed Codenip rules directory")
    try:
        rule = rule_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read the installed Codenip rule {page}") from exc
    header = re.match(r"---\nid: ([A-Z]+[0-9]+)\n---\n", rule)
    if header is None:
        raise ValueError(f"Codenip rule {page} is missing its rule ID")
    rule_id = header[1]

    return rule_id, (
        f"Apply only Codenip rule {rule_id} to your pinned assignment. Check its conditions and exceptions "
        "against the changed code and relevant callers. Report issues introduced or worsened by the assigned diff. "
        "Return the rule ID, checked scope, and a verdict of findings, clean, or incomplete. "
        "For each finding give the path, line, concrete edit and evidence. Mark each suggestion "
        f"[experimental · {rule_id}]. State any evidence gaps. Freeze your result with task-control `complete` "
        "and end the turn. Send the result to the coordinator for reconciliation.\n\n" + rule
    )
