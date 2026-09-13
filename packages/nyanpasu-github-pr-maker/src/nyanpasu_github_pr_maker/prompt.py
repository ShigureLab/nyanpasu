from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nyanpasu_github.pulls import PullRequestView

    from nyanpasu_github_pr_maker.models import GitHubPrMakerConfig, PullRequestPlan, PullRequestPublishMetadata


def build_pr_maker_instructions(
    config: GitHubPrMakerConfig, *, dry_run: bool, auth_instructions: tuple[str, ...] = ()
) -> str:
    parts = [
        (Path(__file__).with_name("instructions") / "pr-maker.md").read_text(encoding="utf-8").strip(),
        f"Publication mode: {'dry run' if dry_run else 'implementation and PR publication enabled'}.",
    ]
    if auth_instructions:
        parts.extend(["GitHub authentication:", *auth_instructions])
    if config.extra_prompt:
        parts.extend(["Additional plugin instructions:", config.extra_prompt.strip()])
    return "\n\n".join(parts) + "\n"


def build_pr_maker_prompt(
    *,
    plan: PullRequestPlan,
    worktree_placeholder: str = "{{NYANPASU_WORKTREE}}",
) -> str:
    parts = [
        f"Implement a new PR in {plan.repo}.",
        f"Base branch: {plan.base_branch}",
        f"Worktree: {worktree_placeholder}",
        f"Required branch: {plan.branch_name}",
        f"PR title: {plan.title}",
        f"Draft: {plan.draft}; dry run: {plan.dry_run}",
        "",
        "Task:",
        plan.task.strip(),
        "",
        "Requested PR body:",
        plan.body.strip(),
    ]
    if plan.commit_message:
        parts.extend(["", f"Suggested commit message: {plan.commit_message}"])
    if plan.labels:
        parts.append(f"Requested labels: {', '.join(plan.labels)}")
    if plan.git_author_name or plan.git_author_email:
        parts.append(
            f"Git author: {plan.git_author_name or '(git default)'} <{plan.git_author_email or '(git default)'}>"
        )
    return "\n".join(parts) + "\n"


def build_pr_follow_up_prompt(
    *,
    publish: PullRequestPublishMetadata,
    pr: PullRequestView,
    include_original_task: bool = False,
    worktree_placeholder: str = "{{NYANPASU_WORKTREE}}",
) -> str:
    parts = [
        f"Continue the existing PR: {pr.url}",
        f"Repository: {publish.repo}; working branch: {publish.branch_name}",
        f"Target head: {pr.head_sha}",
        f"Worktree: {worktree_placeholder}",
        f"Dry run: {publish.dry_run}",
        "",
        "PR activity or checks changed. Inspect the latest discussion and CI, then address actionable feedback.",
        f"Review decision: {pr.review_decision or 'unknown'}; merge state: {pr.merge_state_status or 'unknown'}",
        f"Failing checks: {', '.join(pr.failing_checks) if pr.failing_checks else 'none'}",
    ]
    if publish.git_author_name or publish.git_author_email:
        parts.append(
            f"Git author: {publish.git_author_name or '(git default)'} <{publish.git_author_email or '(git default)'}>"
        )
    if include_original_task:
        parts.extend(["", "Original task (restored because no previous session is available):", publish.task.strip()])
    return "\n".join(parts) + "\n"


def _title_from_task(task: str) -> str:
    first = " ".join(task.strip().splitlines()[0].split())
    if not first:
        return "Implement requested change"
    return first[:80]
