from __future__ import annotations

from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PullRequestRef,
    RepoSettings,
    ReviewAction,
    ReviewEvent,
)
from nyanpasu_github_reviewer.prompt import build_review_prompt

if TYPE_CHECKING:
    from pathlib import Path


def _event() -> ReviewEvent:
    pr = PullRequestRef(
        repo="ExampleOrg/ExampleRepo",
        number=123,
        url="https://github.com/ExampleOrg/ExampleRepo/pull/123",
        base_ref="main",
        head_ref="feature/cuda132",
        head_sha="abc123def456",
        state="open",
        draft=False,
    )
    return ReviewEvent(
        delivery_id="delivery-1",
        github_event="pull_request:synchronize",
        action=ReviewAction.REVIEW,
        pr=pr,
        after_sha=pr.head_sha,
        raw={},
    )


def _config(tmp_path: Path, *, dry_run: bool = False, post_reviews: bool = True) -> GitHubReviewerConfig:
    return GitHubReviewerConfig(
        repos={"ExampleOrg/ExampleRepo": RepoSettings(local_path=tmp_path / "repo")},
        gh_llm_bin="gh-llm",
        agent_name="ReviewBot",
        github_login="review-bot",
        dry_run=dry_run,
        post_reviews=post_reviews,
    )


def test_review_prompt_defaults_to_chinese_line_level_review(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "GitHub-facing review text must be in Chinese by default" in prompt
    assert "except for the fixed disclosure footer" in prompt
    assert "Agent name: ReviewBot" in prompt
    assert "GitHub login used for writes: review-bot" in prompt
    assert "SOUL:" not in prompt
    assert "Internal review mode: initial_review" in prompt
    assert "PR hygiene" in prompt
    assert "If the PR title or description is clearly missing, misleading, too vague" in prompt
    assert "concise, professional, polite Chinese" in prompt
    assert "Prefer concrete line-level inline review comments" in prompt
    assert "gh-llm pr review-start --pr 123 --repo ExampleOrg/ExampleRepo --head abc123def456" in prompt
    assert "gh-llm pr review-comment" in prompt
    assert "--side RIGHT" in prompt
    assert "--head abc123def456" in prompt
    assert "--body-file" in prompt
    assert "gh-llm pr review-submit --event APPROVE, --event COMMENT, or --event REQUEST_CHANGES" in prompt
    assert "Choose the review-submit `--event` according to the rules above" in prompt
    assert "never print that event name in the review body" in prompt
    assert "summary-only review" in prompt


def test_review_prompt_requires_priority_shields_and_ai_disclosure(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "Review output contract" in prompt
    assert "priority shield label" in prompt
    assert "优先级" in prompt or "priority" in prompt
    assert "https://img.shields.io/badge/P0-blocking-red" in prompt
    assert "https://img.shields.io/badge/P1-high-orange" in prompt
    assert "https://img.shields.io/badge/P2-medium-yellow" in prompt
    assert "https://img.shields.io/badge/P3-low-blue" in prompt
    assert "For every review-comment body, put the selected priority shield and an explicit priority field" in prompt
    assert "**优先级：P1**" in prompt
    assert "concise issue, concrete evidence or impact, and the expected next action" in prompt
    assert "include a GitHub Markdown suggestion block" in prompt
    assert "```suggestion" in prompt
    assert "exactly replace the reviewed line range" in prompt
    assert "include a compact code snippet or pseudocode block" in prompt
    assert "Do not leave a concrete finding with only abstract prose" in prompt
    assert "The final review body is not the place for detailed actionable findings" in prompt
    assert "Keep it short, natural, and maintainers-facing" in prompt
    assert "details are in the inline review comments" in prompt
    assert "do not mechanically report the exact inline comment count" in prompt
    assert "Every final review body must end with this exact disclosure footer" in prompt
    assert "Do not put it at the beginning, edit it, or translate it" in prompt
    assert '<div align="right">' in prompt
    assert "Powered by Nyanpasu with gpt-5.5 xhigh" in prompt
    assert "please check the suggestions carefully" in prompt


def test_review_prompt_hides_internal_trigger_and_submit_event_names(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "Do not expose internal automation details in GitHub-facing text" in prompt
    assert "no trigger names such as `poll`, `synchronize`, or `opened`" in prompt
    assert "no review mode names" in prompt
    assert "raw review-submit event names such as `COMMENT`, `REQUEST_CHANGES`, or `APPROVE`" in prompt
    assert (
        'Do not write boilerplate like "本次处理事件：poll", "本次处理事件：synchronize", '
        '"结论：COMMENT（非阻塞）", "结论：REQUEST_CHANGES", or "结论：APPROVE"'
    ) in prompt
    assert "Use natural review wording" in prompt


def test_review_prompt_requires_inline_review_comments_for_attachable_findings(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "Line-level review contract" in prompt
    assert "each new attachable actionable finding must be a GitHub inline review comment" in prompt
    assert "For initial reviews, use inline comments for attachable findings" in prompt
    assert "For follow-up reviews, first check existing threads from this agent" in prompt
    assert "reuse an unresolved thread when it already covers the same finding" in prompt
    assert "A `thread-reply` may be used when reusing an existing thread" in prompt
    assert "only when the reply adds new information or answers a new user comment" in prompt
    assert "For each actionable line-level finding with a clear local fix" in prompt
    assert "the review-comment body must still include a short code snippet or pseudocode block" in prompt
    assert "Never submit REQUEST_CHANGES with only final body text" in prompt
    assert "thread reply that adds new evidence on a blocking unresolved thread" in prompt
    assert (
        "A final-body-only actionable finding is allowed only when GitHub cannot attach it to any changed line"
        in prompt
    )
    assert "非行级：<reason>" in prompt


def test_review_prompt_reuses_followup_threads_and_suppresses_noise(tmp_path: Path) -> None:
    prompt = build_review_prompt(
        _config(tmp_path),
        _event(),
        "/tmp/worktree",
        review_mode="followup_review",
        previous_head_sha="old-sha",
    )

    assert "Follow-up thread reuse and noise control" in prompt
    assert "unresolved threads from this agent are the canonical place for the same existing finding" in prompt
    assert "Treat findings as duplicates when they point to the same file/line range" in prompt
    assert "do not create a new `review-comment` and do not submit a final review body just to repeat it" in prompt
    assert 'Do not send acknowledgements such as "still unresolved", "same as before", "please fix"' in prompt
    assert "Explicit user triggers still deserve a response" in prompt
    assert "direct PR comment mentions" in prompt
    assert "review-thread replies involving this agent" in prompt
    assert "only when the review policy below says a GitHub-visible review is needed" in prompt
    assert "skip GitHub review submission when there is no new actionable finding" in prompt


def test_review_prompt_uses_default_deep_review_process(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "Default deep review process" in prompt
    assert "privately build a risk map" in prompt
    assert "entry points, public API or data format changes" in prompt
    assert "concurrency, performance, security, resource-lifetime, or error-handling surface" in prompt
    assert "force context expansion before posting" in prompt
    assert "complete surrounding function/class" in prompt
    assert "callers/callees/usages" in prompt
    assert "Prefer `rg` for repository search" in prompt
    assert "correctness and boundary cases" in prompt
    assert "API/backward compatibility and data/schema contracts" in prompt
    assert "resource lifetime, error handling, and cleanup" in prompt
    assert "performance, memory, and concurrency" in prompt
    assert "security and data handling" in prompt
    assert "tests, CI, docs, and PR hygiene" in prompt
    assert "Use a two-pass finding workflow" in prompt
    assert "Pass 1 collects candidate findings" in prompt
    assert "Pass 2 tries to disprove each candidate" in prompt
    assert "Post only verified or strongly evidenced findings" in prompt
    assert "Do not dump the private risk map, checklist, or rejected candidates" in prompt


def test_review_prompt_hides_raw_thread_ids_from_user_facing_text(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "Do not expose GitHub GraphQL node ids or opaque thread ids" in prompt
    assert "PRRT_*" in prompt
    assert "PRR_*" in prompt
    assert "internal command handles only" in prompt
    assert "normal GitHub permalink to the visible review comment" in prompt
    assert "Do not use the raw thread id as the locator" in prompt


def test_review_prompt_uses_configured_language_and_collapse_authors(tmp_path: Path) -> None:
    config = _config(tmp_path).model_copy(
        update={
            "review_language": "Japanese",
            "auto_collapse_author_logins": ("repo-bot", "CI Bot"),
        }
    )
    prompt = build_review_prompt(config, _event(), "/tmp/worktree")

    assert "GitHub-facing review text must be in Japanese by default" in prompt
    assert "--auto-collapse-author repo-bot" in prompt
    assert "--auto-collapse-author 'CI Bot'" in prompt


def test_review_prompt_followup_mode_focuses_on_prior_review(tmp_path: Path) -> None:
    prompt = build_review_prompt(
        _config(tmp_path),
        _event(),
        "/tmp/worktree",
        review_mode="followup_review",
        previous_head_sha="old-sha",
    )

    assert "Internal review mode: followup_review" in prompt
    assert "Previous reviewed head sha from local state: old-sha." in prompt
    assert "previous reviews and review threads written by GitHub login `review-bot`" in prompt
    assert "resolved, partially resolved, unresolved, or superseded" in prompt
    assert "reuse the existing thread as the canonical discussion" in prompt
    assert "Do not open another inline comment on the same file/line" in prompt
    assert "Reply to an existing thread only when you have new information" in prompt
    assert "For automatic follow-up triggers with no new findings" in prompt
    assert "Final body should read like a natural follow-up note" in prompt
    assert "If a prior PR title/description suggestion is still unaddressed" in prompt
    assert "Every final review body must end with this exact disclosure footer" in prompt
    assert "For every review-comment body, put the selected priority shield and an explicit priority field" in prompt
    assert "avoid duplicate inline comments unless the code/evidence/fix has materially changed" in prompt
    assert "do not post a duplicate inline comment" in prompt


def test_review_prompt_request_changes_only_for_blockers_and_comment_for_non_blockers(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "Use REQUEST_CHANGES only when there is at least one blocking P0/P1 finding" in prompt
    assert "Use COMMENT when there are only non-blocking findings" in prompt
    assert "use APPROVE with a concise acceptable-change conclusion" in prompt
    assert "Do not submit REQUEST_CHANGES for PR hygiene, CI/template status, or P2/P3 findings alone" in prompt
    assert "Do not use REQUEST_CHANGES merely to repeat an old unresolved thread" in prompt
    assert "Choose the review-submit `--event` according to the rules above" in prompt
    assert "If a GitHub-visible review is warranted and there are no findings" in prompt


def test_review_prompt_requires_pr_title_and_description_suggestions(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "If the PR title or description is clearly missing, misleading, too vague" in prompt
    assert "include a concrete suggestion in the review" in prompt
    assert "PR title and description issues normally cannot be attached to a changed diff line" in prompt
    assert "include a final-body-only PR hygiene suggestion with a concrete replacement" in prompt


def test_review_prompt_can_disable_request_changes_without_disabling_approve(tmp_path: Path) -> None:
    config = _config(tmp_path).model_copy(update={"request_changes_on_findings": False})
    prompt = build_review_prompt(config, _event(), "/tmp/worktree")

    assert "Do not use REQUEST_CHANGES" in prompt
    assert "use APPROVE when there are no findings" in prompt
    assert "gh-llm pr review-submit --event APPROVE, --event COMMENT, or --event REQUEST_CHANGES" in prompt
    assert "Submit at most one final review" in prompt
    assert "never print that event name in the review body" in prompt


def test_review_prompt_includes_event_trigger_context(tmp_path: Path) -> None:
    event = _event()
    event = event.model_copy(
        update={
            "github_event": "issue_comment",
            "raw": {
                "nyanpasu": {
                    "trigger": "mentioned_issue_comment",
                    "trigger_summary": "A PR comment explicitly mentioned `@review-bot`.",
                    "actor": "maintainer",
                    "comment_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123#issuecomment-1",
                    "body_excerpt": "@review-bot please check this",
                }
            },
        }
    )
    prompt = build_review_prompt(_config(tmp_path), event, "/tmp/worktree", review_mode="followup_review")

    assert "Internal trigger context:" in prompt
    assert "trigger kind: mentioned_issue_comment" in prompt
    assert "explicit user request: yes" in prompt
    assert "visible GitHub comment URL" in prompt
    assert "never copy the trigger kind, delivery id, review mode, poll/webhook mechanics" in prompt
    assert "describe only the user-relevant reason for the follow-up when needed" in prompt
    assert "Explicit follow-up request is active" in prompt
    assert "Treat the mention as the main user request for this review turn" in prompt


def test_review_prompt_marks_automatic_followup_for_noise_suppression(tmp_path: Path) -> None:
    event = _event().model_copy(
        update={
            "raw": {
                "nyanpasu": {
                    "trigger": "pull_request_synchronize",
                    "trigger_summary": "Pull request `synchronize` event.",
                }
            },
        }
    )

    prompt = build_review_prompt(_config(tmp_path), event, "/tmp/worktree", review_mode="followup_review")

    assert "trigger kind: pull_request_synchronize" in prompt
    assert "explicit user request: no" in prompt
    assert "Automatic follow-up policy is active" in prompt
    assert "do not post a GitHub-visible update" in prompt


def test_review_prompt_includes_pr_review_body_mention_context(tmp_path: Path) -> None:
    event = _event().model_copy(
        update={
            "github_event": "pull_request_review",
            "raw": {
                "nyanpasu": {
                    "trigger": "mentioned_pull_request_review",
                    "trigger_summary": "A PR review body explicitly mentioned `@review-bot`.",
                    "comment_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123#pullrequestreview-30",
                    "body_excerpt": "@review-bot please check this review",
                }
            },
        }
    )

    prompt = build_review_prompt(_config(tmp_path), event, "/tmp/worktree", review_mode="followup_review")

    assert "trigger kind: mentioned_pull_request_review" in prompt
    assert "explicit PR review-body mention" in prompt
    assert "pullrequestreview-30" in prompt


def test_review_prompt_includes_coalesced_event_context(tmp_path: Path) -> None:
    event = _event().model_copy(
        update={
            "raw": {
                "nyanpasu": {
                    "trigger": "pull_request_synchronize",
                    "trigger_summary": "Pull request `synchronize` event.",
                    "coalesced_events": [
                        {
                            "delivery_id": "delivery-comment",
                            "trigger": "mentioned_issue_comment",
                            "trigger_summary": "A PR comment explicitly mentioned `@review-bot`.",
                            "comment_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123#issuecomment-1",
                        }
                    ],
                }
            },
        }
    )

    prompt = build_review_prompt(_config(tmp_path), event, "/tmp/worktree")

    assert "Coalesced events also included in this review turn" in prompt
    assert "mentioned_issue_comment (delivery-comment)" in prompt
    assert "issuecomment-1" in prompt


def test_review_prompt_does_not_hardcode_repo_specific_authors(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path), _event(), "/tmp/worktree")

    assert "ExampleOrg-bot" not in prompt
    assert "Example-CI-Bot" not in prompt


def test_review_prompt_dry_run_disables_posting(tmp_path: Path) -> None:
    prompt = build_review_prompt(_config(tmp_path, dry_run=True, post_reviews=False), _event(), "/tmp/worktree")

    assert "DRY RUN: do not run review-comment or review-submit" in prompt
    assert "report the review you would have posted" in prompt
