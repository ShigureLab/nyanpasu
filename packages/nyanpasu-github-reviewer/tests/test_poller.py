from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nyanpasu_github_reviewer.models import GitHubReviewerConfig, RepoSettings, ReviewAction, ReviewEvent
from nyanpasu_github_reviewer.poller import GitHubEventsPoller, event_from_repo_event
from nyanpasu_github_reviewer.store import GitHubReviewerStore

if TYPE_CHECKING:
    from pathlib import Path


class FakeAgent:
    def __init__(self) -> None:
        self.events: list[ReviewEvent] = []

    async def submit(self, event: ReviewEvent) -> dict[str, Any]:
        self.events.append(event)
        return {"accepted": True, "delivery_id": event.delivery_id, "action": event.action.value}

    async def run_now(self, event: ReviewEvent) -> dict[str, Any]:
        self.events.append(event)
        return {"accepted": True, "delivery_id": event.delivery_id, "action": event.action.value}


def _config(tmp_path: Path) -> GitHubReviewerConfig:
    return GitHubReviewerConfig(
        repos={"ExampleOrg/ExampleRepo": RepoSettings(local_path=tmp_path / "repo")},
        github_login="review-bot",
        dry_run=True,
        post_reviews=False,
    )


def _config_with_repos(tmp_path: Path, repos: list[str]) -> GitHubReviewerConfig:
    return GitHubReviewerConfig(
        repos={repo: RepoSettings(local_path=tmp_path / repo.replace("/", "-")) for repo in repos},
        github_login="review-bot",
        dry_run=True,
        post_reviews=False,
    )


def _repo_event(
    event_id: int | str,
    event_type: str,
    *,
    created_at: str,
    repo: str = "ExampleOrg/ExampleRepo",
    payload: dict[str, Any],
    actor: str = "maintainer",
) -> dict[str, Any]:
    return {
        "id": str(event_id),
        "type": event_type,
        "actor": {"login": actor},
        "repo": {"name": repo},
        "payload": payload,
        "public": True,
        "created_at": created_at,
    }


def _pr_payload(
    action: str = "synchronize",
    *,
    number: int = 1,
    sha: str = "abc123",
    base_ref: str = "main",
    state: str = "open",
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}",
            "state": state,
            "draft": draft,
            "base": {"ref": base_ref},
            "head": {"ref": "feature", "sha": sha},
        },
    }


def _issue_comment_payload(
    body: str = "@review-bot please review",
    *,
    number: int = 1,
    action: str = "created",
    comment_id: int = 10,
) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": number,
            "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}",
            "state": "open",
            "pull_request": {"url": f"https://api.github.com/repos/ExampleOrg/ExampleRepo/pulls/{number}"},
        },
        "comment": {
            "id": comment_id,
            "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}#issuecomment-{comment_id}",
            "body": body,
            "user": {"login": "maintainer"},
            "created_at": "2026-05-30T10:05:00Z",
            "updated_at": "2026-05-30T10:05:00Z",
        },
    }


def _review_comment_payload(
    body: str = "请看下这个回复",
    *,
    number: int = 1,
    action: str = "created",
    comment_id: int = 20,
    in_reply_to_id: int | None = 10,
) -> dict[str, Any]:
    payload = _pr_payload(action=action, number=number)
    payload["comment"] = {
        "id": comment_id,
        "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}#discussion_r{comment_id}",
        "body": body,
        "path": "src/foo.cc",
        "line": 42,
        "original_line": 42,
        "in_reply_to_id": in_reply_to_id,
        "pull_request_review_id": 30,
        "user": {"login": "maintainer"},
        "created_at": "2026-05-30T10:05:00Z",
        "updated_at": "2026-05-30T10:05:00Z",
    }
    return payload


def _review_payload(
    body: str = "@review-bot 看一下",
    *,
    number: int = 1,
    action: str = "created",
    review_id: int = 30,
) -> dict[str, Any]:
    payload = _pr_payload(action=action, number=number)
    payload["review"] = {
        "id": review_id,
        "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}#pullrequestreview-{review_id}",
        "body": body,
        "state": "commented",
        "user": {"login": "maintainer"},
        "submitted_at": "2026-05-30T10:05:00Z",
    }
    return payload


@pytest.mark.anyio
async def test_events_poller_cold_start_baselines_current_repo_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    events = [
        _repo_event(1, "IssueCommentEvent", created_at="2026-05-30T10:00:00Z", payload=_issue_comment_payload()),
        _repo_event(2, "PullRequestEvent", created_at="2026-05-30T10:05:00Z", payload=_pr_payload("opened")),
    ]

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=lambda *_: events)

    result = await poller.run_once()

    assert (result.submitted, result.duplicates, result.ignored, result.baselined) == (0, 0, 2, 2)
    assert agent.events == []
    cursor = store.get_poll_event_cursor("ExampleOrg/ExampleRepo")
    assert cursor is not None
    assert cursor.last_event_created_at == "2026-05-30T10:05:00Z"
    assert cursor.cursor_event_ids == ("2",)


@pytest.mark.anyio
async def test_events_poller_processes_events_after_previous_cursor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    batches = [
        [_repo_event(1, "PullRequestEvent", created_at="2026-05-30T10:00:00Z", payload=_pr_payload("opened"))],
        [
            _repo_event(1, "PullRequestEvent", created_at="2026-05-30T10:00:00Z", payload=_pr_payload("opened")),
            _repo_event(2, "IssueCommentEvent", created_at="2026-05-30T10:05:00Z", payload=_issue_comment_payload()),
            _repo_event(3, "PullRequestEvent", created_at="2026-05-30T10:06:00Z", payload=_pr_payload("synchronize")),
        ],
        [
            _repo_event(2, "IssueCommentEvent", created_at="2026-05-30T10:05:00Z", payload=_issue_comment_payload()),
            _repo_event(3, "PullRequestEvent", created_at="2026-05-30T10:06:00Z", payload=_pr_payload("synchronize")),
        ],
    ]

    def list_repo_events(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=list_repo_events)

    await poller.run_once()
    second = await poller.run_once()
    repeated = await poller.run_once()

    assert second.submitted == 2
    assert repeated.submitted == 0
    assert [event.github_event for event in agent.events] == ["issue_comment", "pull_request"]
    assert [event.raw["nyanpasu"]["source"] for event in agent.events] == ["events_poll", "events_poll"]
    assert agent.events[0].raw["nyanpasu"]["trigger"] == "mentioned_issue_comment"
    assert agent.events[1].raw["nyanpasu"]["trigger"] == "pull_request_synchronize"


@pytest.mark.anyio
async def test_events_poller_handles_same_timestamp_events_not_in_cursor_ids(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    timestamp = "2026-05-30T10:00:00Z"
    batches = [
        [_repo_event(1, "PullRequestEvent", created_at=timestamp, payload=_pr_payload("opened"))],
        [
            _repo_event(1, "PullRequestEvent", created_at=timestamp, payload=_pr_payload("opened")),
            _repo_event(2, "IssueCommentEvent", created_at=timestamp, payload=_issue_comment_payload()),
        ],
    ]

    def list_repo_events(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=list_repo_events)

    await poller.run_once()
    result = await poller.run_once()

    assert result.submitted == 1
    assert len(agent.events) == 1
    assert agent.events[0].delivery_id == "events-poll-ExampleOrg-ExampleRepo-2"


@pytest.mark.anyio
async def test_events_poller_defaults_to_unlimited_interesting_events_per_cycle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    events = [_repo_event(0, "PullRequestEvent", created_at="2026-05-30T10:00:00Z", payload=_pr_payload("opened"))]
    events.extend(
        _repo_event(
            event_id,
            "IssueCommentEvent",
            created_at=f"2026-05-30T10:{event_id:02d}:00Z",
            payload=_issue_comment_payload(comment_id=event_id),
        )
        for event_id in range(1, 13)
    )
    batches = [events[:1], events]

    def list_repo_events(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=list_repo_events)

    await poller.run_once()
    result = await poller.run_once()

    assert result.submitted == 12
    assert len(agent.events) == 12


@pytest.mark.anyio
async def test_events_poller_can_limit_interesting_events_per_cycle(tmp_path: Path) -> None:
    config = _config(tmp_path).model_copy(update={"poll_max_events_per_cycle": 2})
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    events = [_repo_event(0, "PullRequestEvent", created_at="2026-05-30T10:00:00Z", payload=_pr_payload("opened"))]
    events.extend(
        _repo_event(
            event_id,
            "IssueCommentEvent",
            created_at=f"2026-05-30T10:{event_id:02d}:00Z",
            payload=_issue_comment_payload(comment_id=event_id),
        )
        for event_id in range(1, 5)
    )
    batches = [events[:1], events, events]

    def list_repo_events(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=list_repo_events)

    await poller.run_once()
    first = await poller.run_once()
    second = await poller.run_once()

    assert first.submitted == 2
    assert second.submitted == 2
    assert len(agent.events) == 4


@pytest.mark.anyio
async def test_events_poller_skips_previously_processed_delivery_ids_without_consuming_event_limit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path).model_copy(update={"poll_max_events_per_cycle": 1})
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    events = [_repo_event(0, "PullRequestEvent", created_at="2026-05-30T10:00:00Z", payload=_pr_payload("opened"))]
    events.extend(
        [
            _repo_event(1, "IssueCommentEvent", created_at="2026-05-30T10:01:00Z", payload=_issue_comment_payload()),
            _repo_event(
                2,
                "IssueCommentEvent",
                created_at="2026-05-30T10:02:00Z",
                payload=_issue_comment_payload(comment_id=11),
            ),
        ]
    )
    processed = {"events-poll-ExampleOrg-ExampleRepo-1"}
    batches = [events[:1], events]

    def list_repo_events(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        event_status=lambda delivery_id: "completed" if delivery_id in processed else None,
        list_repo_events=list_repo_events,
    )

    await poller.run_once()
    result = await poller.run_once()

    assert result.duplicates == 1
    assert result.submitted == 1
    assert len(agent.events) == 1
    assert agent.events[0].delivery_id == "events-poll-ExampleOrg-ExampleRepo-2"


@pytest.mark.anyio
async def test_events_poller_ignores_non_mentions_and_unsupported_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    events = [
        _repo_event(1, "PullRequestEvent", created_at="2026-05-30T10:00:00Z", payload=_pr_payload("opened")),
        _repo_event(
            2,
            "IssueCommentEvent",
            created_at="2026-05-30T10:01:00Z",
            payload=_issue_comment_payload(body="ordinary comment"),
        ),
        _repo_event(3, "PushEvent", created_at="2026-05-30T10:02:00Z", payload={"ref": "refs/heads/main"}),
    ]
    batches = [events[:1], events]

    def list_repo_events(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=list_repo_events)

    await poller.run_once()
    result = await poller.run_once()

    assert result.submitted == 0
    assert result.ignored == 2
    assert agent.events == []


@pytest.mark.anyio
async def test_events_poller_skips_failed_repo_and_continues_other_repos(tmp_path: Path) -> None:
    repos = ["ExampleOrg/ExampleRepo", "ExampleOrg/SecondRepo"]
    config = _config_with_repos(tmp_path, repos)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()

    def list_repo_events(_: GitHubReviewerConfig, repo: str) -> list[dict[str, Any]]:
        if repo == "ExampleOrg/ExampleRepo":
            raise RuntimeError("temporary gh failure")
        return [_repo_event(1, "PullRequestEvent", repo=repo, created_at="2026-05-30T10:00:00Z", payload=_pr_payload())]

    poller = GitHubEventsPoller(config, store=store, agent=agent, list_repo_events=list_repo_events)

    result = await poller.run_once()

    assert result.repos == tuple(repos)
    assert result.baselined == 1
    assert result.ignored == 1
    assert store.get_poll_event_cursor("ExampleOrg/ExampleRepo") is None
    assert store.get_poll_event_cursor("ExampleOrg/SecondRepo") is not None


def test_event_from_repo_event_parses_review_comment_and_review_mentions() -> None:
    review_comment = event_from_repo_event(
        _repo_event(
            1,
            "PullRequestReviewCommentEvent",
            created_at="2026-05-30T10:00:00Z",
            payload=_review_comment_payload(),
        ),
        delivery_id="events-poll-ExampleOrg-ExampleRepo-1",
        agent_login="review-bot",
    )
    review = event_from_repo_event(
        _repo_event(
            2,
            "PullRequestReviewEvent",
            created_at="2026-05-30T10:01:00Z",
            payload=_review_payload(),
        ),
        delivery_id="events-poll-ExampleOrg-ExampleRepo-2",
        agent_login="review-bot",
    )

    assert review_comment is not None
    assert review_comment.action is ReviewAction.REVIEW
    assert review_comment.github_event == "pull_request_review_comment"
    assert review_comment.raw["nyanpasu"]["source"] == "events_poll"
    assert review is not None
    assert review.action is ReviewAction.REVIEW
    assert review.github_event == "pull_request_review"
    assert review.raw["nyanpasu"]["source"] == "events_poll"
