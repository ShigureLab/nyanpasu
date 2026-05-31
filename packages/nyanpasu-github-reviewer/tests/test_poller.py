from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PullRequestSnapshot,
    RepoSettings,
    ReviewAction,
    ReviewEvent,
)
from nyanpasu_github_reviewer.poller import GitHubEventsPoller, event_from_pr_timeline_item, event_from_repo_event
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


def _pull_request_api_item(
    number: int = 1,
    *,
    node_id: str = "PR_1",
    sha: str = "abc123",
    updated_at: str = "2026-05-30T10:00:00Z",
    base_ref: str = "main",
    head_ref: str = "feature",
    head_repo: str = "Contributor/ExampleRepo",
    state: str = "open",
    draft: bool = False,
    title: str = "Example PR",
    body: str = "Example body",
    created_at: str = "2026-05-30T09:59:00Z",
) -> dict[str, Any]:
    return {
        "number": number,
        "node_id": node_id,
        "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/{number}",
        "state": state,
        "draft": draft,
        "title": title,
        "body": body,
        "created_at": created_at,
        "updated_at": updated_at,
        "base": {"ref": base_ref},
        "head": {
            "ref": head_ref,
            "sha": sha,
            "repo": {"full_name": head_repo},
        },
    }


def _timeline_issue_comment_item(
    body: str = "@review-bot please review",
    *,
    comment_id: int = 100,
    created_at: str = "2026-05-30T10:05:00Z",
    updated_at: str = "2026-05-30T10:05:00Z",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "node_id": f"IC_{comment_id}",
        "event": "commented",
        "issue_url": "https://api.github.com/repos/ExampleOrg/ExampleRepo/issues/1",
        "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/1#issuecomment-{comment_id}",
        "body": body,
        "user": {"login": "maintainer"},
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _timeline_review_comment_item(
    body: str = "回复一下",
    *,
    comment_id: int = 200,
    created_at: str = "2026-05-30T10:05:00Z",
    updated_at: str = "2026-05-30T10:05:00Z",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "node_id": f"RC_{comment_id}",
        "event": "line-commented",
        "html_url": f"https://github.com/ExampleOrg/ExampleRepo/pull/1#discussion_r{comment_id}",
        "body": body,
        "user": {"login": "maintainer"},
        "path": "src/foo.cc",
        "line": 42,
        "original_line": 42,
        "in_reply_to_id": 199,
        "pull_request_review_id": 30,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _snapshot_from_test_pr() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repo="ExampleOrg/ExampleRepo",
        number=1,
        node_id="PR_1",
        url="https://github.com/ExampleOrg/ExampleRepo/pull/1",
        state="open",
        draft=False,
        base_ref="main",
        head_ref="feature",
        head_repo="Contributor/ExampleRepo",
        head_sha="abc123",
        title_hash="title",
        body_hash="body",
        created_at="2026-05-30T09:59:00Z",
        updated_at="2026-05-30T10:00:00Z",
    )


@pytest.mark.anyio
async def test_events_poller_cold_start_baselines_current_repo_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    events = [
        _repo_event(1, "IssueCommentEvent", created_at="2026-05-30T10:00:00Z", payload=_issue_comment_payload()),
        _repo_event(2, "PullRequestEvent", created_at="2026-05-30T10:05:00Z", payload=_pr_payload("opened")),
    ]

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

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

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=list_repo_events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

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

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=list_repo_events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

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

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=list_repo_events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

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

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=list_repo_events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

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
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
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

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=list_repo_events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

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

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=list_repo_events,
        list_pull_requests=lambda *_: [],
        list_pull_request_timeline=lambda *_: [],
    )

    result = await poller.run_once()

    assert result.repos == tuple(repos)
    assert result.baselined == 1
    assert result.ignored == 1
    assert store.get_poll_event_cursor("ExampleOrg/ExampleRepo") is None
    assert store.get_poll_event_cursor("ExampleOrg/SecondRepo") is not None


@pytest.mark.anyio
async def test_pr_state_poll_synthesizes_synchronize_for_fork_head_update(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    batches = [
        [_pull_request_api_item(1, sha="old", updated_at="2026-05-30T10:00:00Z")],
        [_pull_request_api_item(1, sha="new", updated_at="2026-05-30T10:05:00Z")],
        [_pull_request_api_item(1, sha="new", updated_at="2026-05-30T10:05:00Z")],
    ]

    def list_pull_requests(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=list_pull_requests,
        list_pull_request_timeline=lambda *_: [],
    )

    first = await poller.run_once()
    second = await poller.run_once()
    repeated = await poller.run_once()

    assert first.submitted == 0
    assert first.baselined == 1
    assert second.submitted == 1
    assert repeated.submitted == 0
    assert len(agent.events) == 1
    event = agent.events[0]
    assert event.github_event == "pull_request"
    assert event.pr is not None
    assert event.pr.number == 1
    assert event.after_sha == "new"
    assert event.raw["nyanpasu"]["trigger"] == "pull_request_synchronize"
    assert event.delivery_id.startswith("synthetic-pr-state-ExampleOrg-ExampleRepo-1-synchronize-")


@pytest.mark.anyio
async def test_pr_state_poll_does_not_open_old_pr_first_seen_after_cursor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    batches = [
        [_pull_request_api_item(2, node_id="PR_2", updated_at="2026-05-30T10:00:00Z")],
        [
            _pull_request_api_item(
                3,
                node_id="PR_3",
                created_at="2026-05-30T09:00:00Z",
                updated_at="2026-05-30T10:05:00Z",
                title="Old PR edited elsewhere",
            )
        ],
    ]

    def list_pull_requests(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=list_pull_requests,
        list_pull_request_timeline=lambda *_: [],
    )

    await poller.run_once()
    result = await poller.run_once()

    assert result.submitted == 0
    assert agent.events == []
    assert store.get_pr_snapshot("ExampleOrg/ExampleRepo", 3) is not None


@pytest.mark.anyio
async def test_pr_timeline_poll_processes_mentions_after_pr_update(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    pr_batches = [
        [_pull_request_api_item(1, sha="old", updated_at="2026-05-30T10:00:00Z")],
        [_pull_request_api_item(1, sha="old", updated_at="2026-05-30T10:06:00Z", title="Edited")],
        [_pull_request_api_item(1, sha="old", updated_at="2026-05-30T10:06:00Z", title="Edited")],
    ]
    timeline_batches = [
        [_timeline_issue_comment_item(created_at="2026-05-30T10:05:00Z", updated_at="2026-05-30T10:05:00Z")],
        [_timeline_issue_comment_item(created_at="2026-05-30T10:05:00Z", updated_at="2026-05-30T10:05:00Z")],
    ]

    def list_pull_requests(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return pr_batches.pop(0)

    def list_timeline(_: GitHubReviewerConfig, __: str, ___: int) -> list[dict[str, Any]]:
        return timeline_batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=list_pull_requests,
        list_pull_request_timeline=list_timeline,
    )

    await poller.run_once()
    second = await poller.run_once()
    repeated = await poller.run_once()

    assert second.submitted == 2
    assert repeated.submitted == 0
    assert [event.github_event for event in agent.events] == ["issue_comment", "pull_request"]
    assert agent.events[0].raw["nyanpasu"]["trigger"] == "mentioned_issue_comment"
    assert store.get_pr_timeline_cursor("ExampleOrg/ExampleRepo", 1) is not None


@pytest.mark.anyio
async def test_pr_timeline_poll_processes_review_thread_replies_after_pr_update(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    pr_batches = [
        [_pull_request_api_item(1, sha="old", updated_at="2026-05-30T10:00:00Z")],
        [_pull_request_api_item(1, sha="new", updated_at="2026-05-30T10:05:00Z")],
    ]

    def list_pull_requests(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return pr_batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=list_pull_requests,
        list_pull_request_timeline=lambda *_: [_timeline_review_comment_item()],
    )

    await poller.run_once()
    result = await poller.run_once()

    assert result.submitted == 2
    assert [event.github_event for event in agent.events] == ["pull_request", "pull_request_review_comment"]
    assert agent.events[1].raw["nyanpasu"]["trigger"] == "review_thread_comment"


@pytest.mark.anyio
async def test_pr_timeline_poll_skips_old_items_when_cursor_is_created(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    pr_batches = [
        [_pull_request_api_item(1, sha="old", updated_at="2026-05-30T10:00:00Z")],
        [_pull_request_api_item(1, sha="new", updated_at="2026-05-30T10:05:00Z")],
    ]

    def list_pull_requests(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return pr_batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=list_pull_requests,
        list_pull_request_timeline=lambda *_: [
            _timeline_issue_comment_item(
                created_at="2026-05-30T10:00:00Z",
                updated_at="2026-05-30T10:00:00Z",
            )
        ],
    )

    await poller.run_once()
    result = await poller.run_once()

    assert result.submitted == 1
    assert [event.github_event for event in agent.events] == ["pull_request"]
    cursor = store.get_pr_timeline_cursor("ExampleOrg/ExampleRepo", 1)
    assert cursor is not None
    assert cursor.last_item_updated_at == "2026-05-30T10:00:00Z"


def test_event_from_pr_timeline_item_parses_issue_comment_mentions() -> None:
    snapshot = _snapshot_from_test_pr()

    event = event_from_pr_timeline_item(
        _timeline_issue_comment_item(),
        snapshot,
        delivery_id="timeline-1",
        agent_login="review-bot",
    )

    assert event is not None
    assert event.action is ReviewAction.REVIEW
    assert event.github_event == "issue_comment"
    assert event.raw["nyanpasu"]["trigger"] == "mentioned_issue_comment"


def test_event_from_pr_timeline_item_parses_review_thread_replies() -> None:
    snapshot = _snapshot_from_test_pr()

    event = event_from_pr_timeline_item(
        _timeline_review_comment_item(),
        snapshot,
        delivery_id="timeline-1",
        agent_login="review-bot",
    )

    assert event is not None
    assert event.action is ReviewAction.REVIEW
    assert event.github_event == "pull_request_review_comment"
    assert event.raw["nyanpasu"]["trigger"] == "review_thread_comment"


@pytest.mark.anyio
async def test_pr_state_poll_does_not_dedupe_distinct_edited_events_on_same_head(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    agent = FakeAgent()
    batches = [
        [_pull_request_api_item(1, sha="same", updated_at="2026-05-30T10:00:00Z", title="Title 1")],
        [_pull_request_api_item(1, sha="same", updated_at="2026-05-30T10:05:00Z", title="Title 2")],
        [_pull_request_api_item(1, sha="same", updated_at="2026-05-30T10:10:00Z", title="Title 3")],
    ]

    def list_pull_requests(_: GitHubReviewerConfig, __: str) -> list[dict[str, Any]]:
        return batches.pop(0)

    poller = GitHubEventsPoller(
        config,
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=list_pull_requests,
        list_pull_request_timeline=lambda *_: [],
    )

    await poller.run_once()
    first_edit = await poller.run_once()
    second_edit = await poller.run_once()

    assert first_edit.submitted == 1
    assert second_edit.submitted == 1
    assert [event.raw["nyanpasu"]["trigger"] for event in agent.events] == [
        "pull_request_edited",
        "pull_request_edited",
    ]


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
