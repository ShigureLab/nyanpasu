from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nyanpasu.targets import ExecutionOverride

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import SubtaskRequest

CODENIP_KIND = "github_reviewer.codenip"


def prepare_codenip(config: NyanpasuConfig, skill: Path | None, request: SubtaskRequest) -> SubtaskRequest:
    if skill is None:
        raise ValueError("configure codenip_skill before dispatching a Codenip review")
    policy = config.tasks.kinds.get(CODENIP_KIND)
    if policy is None or policy.execution.model is None:
        raise ValueError(f"configure an explicit lightweight model for tasks.kinds.{CODENIP_KIND}.execution")

    page = request.inputs.get("rule")
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

    return request.model_copy(
        update={
            "kind": CODENIP_KIND,
            "execution": ExecutionOverride(),
            "workspace_mode": "worktree",
            "memory_enabled": False,
            "inputs": {"rule_id": rule_id},
            "developer_instructions": (
                f"Apply only Codenip rule {rule_id} to your pinned assignment. Check its conditions and exceptions "
                "against the changed code and relevant callers. Report issues introduced or worsened by the assigned diff. "
                "Return the rule ID, checked scope, and a verdict of findings, clean, or incomplete. "
                "For each finding give the path, line, concrete edit and evidence. Mark each suggestion "
                f"[experimental · {rule_id}]. State any evidence gaps. Freeze your result with task-control `complete` "
                "and end the turn. The parent owns reconciliation and publication.\n\n" + rule
            ),
        }
    )
