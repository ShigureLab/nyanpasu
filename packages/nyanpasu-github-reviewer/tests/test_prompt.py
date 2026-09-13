from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef, RepoSettings, ReviewTrigger
from nyanpasu_github_reviewer.prompt import INSTRUCTIONS_DIR, build_review_instructions, build_review_prompt

if TYPE_CHECKING:
    from pathlib import Path


def _pr(sha: str = "abc123def456") -> PullRequestRef:
    return PullRequestRef(
        repo="ExampleOrg/ExampleRepo",
        number=123,
        url="https://github.com/ExampleOrg/ExampleRepo/pull/123",
        base_ref="main",
        head_ref="feature/cuda132",
        head_sha=sha,
        state="open",
        draft=False,
    )


def _config(tmp_path: Path, **overrides) -> GitHubReviewerConfig:
    return GitHubReviewerConfig(
        repos={"ExampleOrg/ExampleRepo": RepoSettings(local_path=tmp_path / "repo")},
        agent_name="ReviewBot",
        github_login="review-bot",
        **overrides,
    )


def test_session_instructions_are_stable_across_heads_and_turns(tmp_path: Path) -> None:
    config = _config(tmp_path, review_language="Japanese", auto_collapse_author_logins=("repo-bot",))
    instructions = build_review_instructions(config, _pr())

    assert instructions == build_review_instructions(config, _pr("new-head"))
    assert "abc123def456" not in instructions
    assert "ReviewBot" in instructions and "review-bot" in instructions
    assert "Japanese" in instructions and "repo-bot" in instructions
    assert "github-conversation" in instructions and "gh-slate" in instructions
    assert "nyanpasu-review" in instructions and "data.source.head_sha" in instructions
    assert str(INSTRUCTIONS_DIR / "review-output.md") in instructions
    assert len(instructions) < 6000


def test_ordinary_turn_contains_only_current_facts_and_trigger(tmp_path: Path) -> None:
    prompt = build_review_prompt(
        _config(tmp_path),
        _pr(),
        "/tmp/worktree",
        triggers=(ReviewTrigger(kind="pull_request_synchronize", summary="New commits."),),
        has_session=True,
        previous_task_head="old-head",
    )

    assert "Continue reviewing" in prompt
    assert "Target head: abc123def456" in prompt
    assert "Previous task head (not proof of completed review): old-head" in prompt
    assert "/tmp/worktree" in prompt and "New commits." in prompt
    assert "Explicit request: no" in prompt
    assert "github-conversation" not in prompt and "Powered by" not in prompt
    assert len(prompt) < 1000


def test_merged_explicit_requests_are_preserved_without_truncation(tmp_path: Path) -> None:
    triggers = tuple(
        ReviewTrigger(
            kind="mentioned_issue_comment",
            comment_url=f"https://github.com/ExampleOrg/ExampleRepo/pull/123#issuecomment-{index}",
            body_excerpt=f"Request {index}: " + "detail " * 100,
        )
        for index in range(12)
    )
    prompt = build_review_prompt(_config(tmp_path), _pr(), "/tmp/worktree", triggers=triggers)

    assert "Explicit request: yes" in prompt
    for trigger in triggers:
        assert trigger.comment_url in prompt
        assert trigger.body_excerpt in prompt
    assert prompt.count("Target head:") == 1


@pytest.mark.parametrize("overrides", [{"dry_run": True}, {"post_reviews": False}])
def test_read_only_policy_covers_session_and_current_turn(tmp_path: Path, overrides) -> None:
    config = _config(tmp_path, **overrides)
    for text in (
        build_review_instructions(config, _pr()),
        build_review_prompt(config, _pr(), "/tmp/worktree", triggers=()),
    ):
        assert "read-only; do not write to GitHub, including reviews, replies, or the dashboard" in text


def test_request_changes_policy_and_output_reference(tmp_path: Path) -> None:
    instructions = build_review_instructions(_config(tmp_path, request_changes_on_findings=False), _pr())
    output = (INSTRUCTIONS_DIR / "review-output.md").read_text()

    assert "REQUEST_CHANGES is disabled" in instructions
    for priority in range(4):
        assert f"![P{priority}]" in output
    assert "**优先级：P1**" in output
    assert "`suggestion`" in output
    assert "非行级：<reason>" in output
    assert (
        """<div align="right">
   <sup>Powered by Nyanpasu with gpt-6-astra medium, please check the suggestions carefully.</sup>
</div>"""
        in output
    )
