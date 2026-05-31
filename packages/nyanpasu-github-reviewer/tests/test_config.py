from __future__ import annotations

from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.models import GitHubReviewerConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_github_reviewer_config_reads_repos_and_instruction_docs(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    identity_path = tmp_path / "SOUL.md"
    agents_path = tmp_path / "AGENTS.md"

    config = GitHubReviewerConfig.model_validate(
        {
            "github_login": "review-bot",
            "instruction_docs": [{"name": "SOUL.md", "path": str(identity_path)}],
            "review_language": "中文",
            "auto_collapse_author_logins": ["repo-bot", "ci-bot"],
            "poll_interval_seconds": 60,
            "poll_event_pages": 2,
            "poll_max_events_per_cycle": 0,
            "repos": {
                "ExampleOrg/ExampleRepo": {
                    "local_path": str(repo_path),
                    "github_remote": "https://github.com/ExampleOrg/ExampleRepo.git",
                    "base_branches": ["main"],
                    "instruction_docs": [{"name": "AGENTS.md", "path": str(agents_path), "required": False}],
                }
            },
        }
    )

    assert config.github_login == "review-bot"
    assert config.instruction_docs[0].name == "SOUL.md"
    assert config.instruction_docs[0].path == identity_path.resolve()
    assert config.review_language == "中文"
    assert config.auto_collapse_author_logins == ("repo-bot", "ci-bot")
    assert config.poll_interval_seconds == 60
    assert config.poll_event_pages == 2
    assert config.poll_max_events_per_cycle == 0
    assert config.repo_configs["ExampleOrg/ExampleRepo"].local_path == repo_path.resolve()
    assert config.repo_configs["ExampleOrg/ExampleRepo"].base_branches == ("main",)
    repo_docs = config.repos["ExampleOrg/ExampleRepo"].instruction_docs
    assert repo_docs[0].name == "AGENTS.md"
    assert repo_docs[0].path == agents_path.resolve()
    assert repo_docs[0].required is False
