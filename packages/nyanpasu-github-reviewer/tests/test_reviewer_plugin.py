from __future__ import annotations

from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, RepoSettings
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> GitHubReviewerConfig:
    return GitHubReviewerConfig(
        repos={"ExampleOrg/ExampleRepo": RepoSettings(local_path=tmp_path / "repo")},
        github_login="review-bot",
        dry_run=True,
        post_reviews=False,
    )


def _pr_payload(action: str, *, number: int = 1, sha: str = "abc123") -> dict[str, object]:
    return {
        "action": action,
        "repository": {"full_name": "ExampleOrg/ExampleRepo"},
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}",
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": sha},
        },
    }


def test_event_to_task_treats_active_context_as_followup_review(tmp_path: Path) -> None:
    plugin = GitHubReviewerPlugin().bind_for_conversion(
        config=_config(tmp_path),
        context_lookup=lambda _: None,
        active_context_task_lookup=lambda context_key, exclude_task_id: (
            context_key == "github:ExampleOrg/ExampleRepo#1" and exclude_task_id == "delivery-2"
        ),
    )
    event = parse_github_event(
        "pull_request",
        "delivery-2",
        _pr_payload("opened"),
        agent_login="review-bot",
    )

    task = plugin.event_to_task(event)

    assert task.metadata["review_mode"] == "followup_review"
    assert "Internal review mode: followup_review" in task.prompt
