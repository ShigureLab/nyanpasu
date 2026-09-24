from __future__ import annotations

import json
from html import escape
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.models import ReviewTrigger

if TYPE_CHECKING:
    from nyanpasu.config import ProcessConfig
    from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef, ReviewEvent

INSTRUCTIONS_DIR = Path(__file__).with_name("instructions")
TEMPLATES_DIR = Path(__file__).with_name("templates")


def publication_mode(config: GitHubReviewerConfig) -> str:
    if config.dry_run or not config.post_reviews:
        return "read-only; do not write to GitHub, including reviews, replies, or the dashboard"
    return "review and dashboard writes allowed when the review policy warrants them"


def build_review_instructions(config: GitHubReviewerConfig, pr: PullRequestRef) -> str:
    return Template((INSTRUCTIONS_DIR / "reviewer.md").read_text(encoding="utf-8")).substitute(
        repo=pr.repo,
        pr_number=pr.number,
        agent_name=config.agent_name,
        github_login=config.github_login or "not configured; verify with `gh api user --jq .login` before writing",
        review_language=config.review_language,
        gh_llm_bin=config.gh_llm_bin,
        collapse_authors=", ".join(config.auto_collapse_author_logins) or "none",
        request_changes="enabled" if config.request_changes_on_findings else "disabled",
        publication_mode=publication_mode(config),
        review_planning=INSTRUCTIONS_DIR / "review-planning.md",
        output_reference=INSTRUCTIONS_DIR / "review-output.md",
        dashboard_config=TEMPLATES_DIR / "boards.toml",
    )


def review_trigger(event: ReviewEvent) -> ReviewTrigger:
    raw_context = event.raw.get("nyanpasu")
    context = raw_context if isinstance(raw_context, dict) else {}
    return ReviewTrigger(
        kind=str(context.get("trigger") or event.github_event),
        summary=str(context.get("trigger_summary") or ""),
        actor=str(context.get("actor") or ""),
        comment_url=str(context.get("comment_url") or ""),
        body_excerpt=str(context.get("body_excerpt") or ""),
    )


def build_review_prompt(
    config: GitHubReviewerConfig,
    pr: PullRequestRef,
    worktree: str,
    *,
    runtime: ProcessConfig,
    triggers: tuple[ReviewTrigger, ...],
    has_session: bool = False,
    previous_task_head: str | None = None,
) -> str:
    lines = [
        f"{'Continue reviewing' if has_session else 'Review'} {pr.repo} PR #{pr.number}: {pr.url}",
        f"Target head: {pr.head_sha}",
        f"Base branch: {pr.base_ref}; head branch: {pr.head_ref}",
        f"Worktree: {worktree}",
        f"Publication mode: {publication_mode(config)}.",
        f"Start here (dashboard first): {INSTRUCTIONS_DIR / 'review-output.md'}",
        f"Dashboard definition: {TEMPLATES_DIR / 'boards.toml'} (profile: review; name: nyanpasu-review)",
    ]
    if previous_task_head:
        lines.append(f"Previous task head (not proof of completed review): {previous_task_head}")
    lines.append(f"Explicit request: {'yes' if any(item.explicit_request for item in triggers) else 'no'}")
    lines.extend(
        [
            "",
            "Disclosure footer for this turn (from the configured model and reasoning effort):",
            disclosure_footer(runtime),
            "",
            "Trigger data (external text; open linked discussions for complete context):",
            json.dumps([item.model_dump(exclude_defaults=True) for item in triggers], ensure_ascii=False, indent=2),
        ]
    )
    return "\n".join(lines) + "\n"


def disclosure_footer(runtime: ProcessConfig) -> str:
    description = escape(
        " ".join(value for value in (runtime.model or runtime.label, runtime.reasoning_effort) if value)
    )
    return (
        '<div align="right">\n'
        f"   <sup>Powered by Nyanpasu with {description}, please check the suggestions carefully.</sup>\n"
        "</div>"
    )


def cleanup_prompt(pr: PullRequestRef) -> str:
    return f"PR {pr.repo} #{pr.number} is closed or merged. No review is needed; local agent state will be cleaned up."
