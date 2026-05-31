from __future__ import annotations

from typing import Any

from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import ReviewAction


def pr_payload(action: str = "synchronize", *, state: str = "open", draft: bool = False) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "ExampleOrg/ExampleRepo"},
        "pull_request": {
            "number": 123,
            "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123",
            "state": state,
            "draft": draft,
            "base": {"ref": "main"},
            "head": {"ref": "third-party/pybind11-v3", "sha": "f636965"},
        },
    }


def issue_comment_payload(body: str = "@review-bot please review") -> dict[str, Any]:
    return {
        "action": "created",
        "repository": {"full_name": "ExampleOrg/ExampleRepo"},
        "issue": {
            "number": 123,
            "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123",
            "state": "open",
            "pull_request": {"url": "https://api.github.com/repos/ExampleOrg/ExampleRepo/pulls/123"},
        },
        "comment": {
            "id": 1,
            "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123#issuecomment-1",
            "body": body,
            "user": {"login": "maintainer"},
        },
    }


def review_comment_payload(*, body: str = "ping", in_reply_to_id: int | None = 10) -> dict[str, Any]:
    payload = pr_payload()
    payload["action"] = "created"
    payload["comment"] = {
        "id": 20,
        "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123#discussion_r20",
        "body": body,
        "user": {"login": "maintainer"},
        "path": "src/foo.cc",
        "line": 42,
        "in_reply_to_id": in_reply_to_id,
        "pull_request_review_id": 30,
    }
    return payload


def pull_request_review_payload(body: str = "@review-bot please review") -> dict[str, Any]:
    payload = pr_payload()
    payload["action"] = "submitted"
    payload["review"] = {
        "id": 30,
        "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/123#pullrequestreview-30",
        "body": body,
        "user": {"login": "maintainer"},
    }
    return payload


def test_pull_request_synchronize_triggers_review() -> None:
    event = parse_github_event("pull_request", "delivery-1", pr_payload())

    assert event.action is ReviewAction.REVIEW
    assert event.pr is not None
    assert event.pr.repo == "ExampleOrg/ExampleRepo"
    assert event.pr.number == 123
    assert event.after_sha == "f636965"


def test_pull_request_closed_triggers_cleanup() -> None:
    event = parse_github_event("pull_request", "delivery-1", pr_payload("closed", state="closed"))

    assert event.action is ReviewAction.CLEANUP


def test_draft_pull_request_is_ignored() -> None:
    event = parse_github_event("pull_request", "delivery-1", pr_payload(draft=True))

    assert event.action is ReviewAction.IGNORED


def test_pull_request_review_requested_for_agent_triggers_review() -> None:
    payload = pr_payload("review_requested")
    payload["requested_reviewer"] = {"login": "review-bot"}

    event = parse_github_event("pull_request", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.REVIEW
    assert event.raw["nyanpasu"]["trigger"] == "review_requested"


def test_pull_request_review_requested_for_someone_else_is_ignored() -> None:
    payload = pr_payload("review_requested")
    payload["requested_reviewer"] = {"login": "someone-else"}

    event = parse_github_event("pull_request", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.IGNORED


def test_issue_comment_mention_on_pr_triggers_review() -> None:
    event = parse_github_event(
        "issue_comment",
        "delivery-1",
        issue_comment_payload("@review-bot 看一下"),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.REVIEW
    assert event.pr is not None
    assert event.pr.number == 123
    assert event.raw["nyanpasu"]["trigger"] == "mentioned_issue_comment"
    assert event.raw["nyanpasu"]["comment_url"].endswith("#issuecomment-1")


def test_issue_comment_without_mention_is_ignored() -> None:
    event = parse_github_event(
        "issue_comment",
        "delivery-1",
        issue_comment_payload("ordinary comment"),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.IGNORED


def test_pull_request_review_comment_reply_triggers_followup_candidate() -> None:
    event = parse_github_event(
        "pull_request_review_comment",
        "delivery-1",
        review_comment_payload(),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.REVIEW
    assert event.raw["nyanpasu"]["trigger"] == "review_thread_comment"
    assert event.raw["nyanpasu"]["in_reply_to_id"] == 10


def test_pull_request_review_comment_by_agent_is_ignored() -> None:
    payload = review_comment_payload()
    payload["comment"]["user"] = {"login": "review-bot"}  # type: ignore[index]

    event = parse_github_event("pull_request_review_comment", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.IGNORED


def test_pull_request_review_body_mention_triggers_review() -> None:
    event = parse_github_event(
        "pull_request_review",
        "delivery-1",
        pull_request_review_payload("@review-bot 看一下"),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.REVIEW
    assert event.raw["nyanpasu"]["trigger"] == "mentioned_pull_request_review"
    assert event.raw["nyanpasu"]["comment_url"].endswith("#pullrequestreview-30")


def test_pull_request_review_without_mention_is_ignored() -> None:
    event = parse_github_event(
        "pull_request_review",
        "delivery-1",
        pull_request_review_payload("ordinary review"),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.IGNORED
