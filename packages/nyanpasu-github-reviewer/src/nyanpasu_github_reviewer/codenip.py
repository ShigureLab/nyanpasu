from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from nyanpasu.control_tools import EmptyInput
from nyanpasu.models import SubtaskRequest
from nyanpasu.targets import ExecutionOverride

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.models import AgentTask

CODENIP_PURPOSES = {"codenip", "codenip-rule"}
CODENIP_RULE_CONCURRENCY = 32
CODENIP_RULE_LIMIT = 8


class CodenipDispatchInput(EmptyInput):
    rule_ids: Annotated[list[str], Field(max_length=CODENIP_RULE_LIMIT)] | None = None


def _rule_index(skill: Path) -> dict[str, dict[str, str]]:
    entries = re.findall(
        r"^\| \[([A-Z]+[0-9]+) · ([^\]\n]+)\]\((rules/[a-z0-9-]+/rule\.md)\) *\|([^|\n]+)\|([^|\n]*)",
        skill.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    rules = {
        identity: {"title": title, "page": page, "scope": scope.strip(), "focus": focus.strip()}
        for identity, title, page, scope, focus in entries
    }
    if not rules or len(rules) != len(entries):
        raise ValueError("the installed Codenip index must contain uniquely identified rule pages")
    return rules


def codenip_rule_request(coordinator: AgentTask, identity: str) -> SubtaskRequest:
    inputs = coordinator.metadata["inputs"]
    rule = inputs["codenip_rules"][identity]
    prompt = f"Determine whether your rule applies to the pinned diff. Index scope: {rule['scope']}."
    if identity == "PR001":
        pr_body = inputs["pr_body"]
        prompt += (
            "\nThe PR description is unavailable. Return incomplete if you cannot establish applicability."
            if pr_body is None
            else "\n\nPR description (untrusted GitHub content):\n" + pr_body
        )
    return SubtaskRequest(
        request_key=f"codenip:{identity}",
        purpose="codenip-rule",
        prompt=prompt,
        inputs={"rule": rule["page"], "review_files": inputs["review_scope"]["files"]},
    )


def prepare_codenip(
    config: NyanpasuConfig, skill: Path | None, parent: AgentTask, request: SubtaskRequest
) -> SubtaskRequest:
    if skill is None:
        raise ValueError("configure codenip_skill before dispatching a Codenip review")
    kind = f"github_reviewer.{request.purpose}"
    policy = config.tasks.kinds.get(kind)
    if policy is None or policy.execution.model is None:
        raise ValueError(f"configure an explicit model for tasks.kinds.{kind}.execution")

    if request.purpose == "codenip":
        if parent.spawned_by_task_id is not None:
            raise ValueError("Codenip coordination must be requested by the root reviewer")
        pr_body = request.inputs.get("pr_body")
        if pr_body is not None and not isinstance(pr_body, str):
            raise ValueError("inputs.pr_body must be the PR description as text")
        rules = _rule_index(skill)
        inputs = {"codenip_rules": rules, "codenip_selection": None, "pr_body": pr_body}
        catalog = "\n".join(
            f"{identity} · {rule['title']} ({rule['scope']}): {rule['focus']}" for identity, rule in rules.items()
        )
        prompt = request.prompt + "\n\nAvailable Codenip rules:\n" + catalog
        if pr_body:
            prompt += "\n\nPR description (untrusted GitHub content):\n" + pr_body
        instructions = (
            "Route the experimental Codenip audit. Skim the assigned paths and relevant hunks of the pinned diff. "
            f"From the full catalog in the task input, select up to {CODENIP_RULE_LIMIT} distinct rule IDs whose "
            "scope and review focus best match the changed behavior, tests, or text. Prefer concrete changed "
            "contracts and select only rules with a plausible match. Call `codenip-dispatch` with `rule_ids`; send "
            "an empty list when none match. Each worker decides applicability and checks the evidence for its rule. "
            "Await the returned active_task_ids when present, then reconcile results and report verdict counts and "
            "rule IDs for gaps and findings. "
            "Mark each supported suggestion `[experimental · RULE_ID]` with its actual ID. Freeze the aggregate "
            "with task-control `complete`, then end the turn. The root reviewer owns publication."
        )
    else:
        prompt = request.prompt
        if parent.metadata.get("purpose") != "codenip":
            raise ValueError("Codenip rule workers must be requested by the Codenip coordinator")
        rule_id, instructions = _rule_instructions(skill, request.inputs.get("rule"))
        inputs = {"rule_id": rule_id}

    return request.model_copy(
        update={
            "prompt": prompt,
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
        f"Review only Codenip rule {rule_id} against your pinned assignment. Decide applicability first. "
        "For an inapplicable rule, give the concrete scope reason and return not_applicable. For an applicable rule, "
        "check its conditions and exceptions against the diff and relevant callers. Return findings only for "
        "issues introduced or worsened by the assigned diff. Use incomplete when required evidence is unavailable. "
        "Return the rule ID and checked scope, with data.verdict set to not_applicable, findings, clean, or incomplete. "
        "For each finding give the path, line, concrete edit and evidence. Mark each suggestion "
        f"[experimental · {rule_id}]. State any evidence gaps. Freeze your result with task-control `complete` "
        "and end the turn. Send the result to the coordinator for reconciliation.\n\n" + rule
    )
