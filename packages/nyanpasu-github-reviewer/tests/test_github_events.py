from __future__ import annotations

from typing import Any

import pytest

from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import ReviewAction
from nyanpasu_github_reviewer.prompt import review_trigger


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


@pytest.mark.parametrize("action", ["created", "edited"])
@pytest.mark.parametrize("body", ["/review", "please /review this PR", "看一下\n/REVIEW", "(/review)"])
def test_issue_comment_review_command_triggers_explicit_review(action: str, body: str) -> None:
    payload = issue_comment_payload(body)
    payload["action"] = action

    event = parse_github_event("issue_comment", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.REVIEW
    assert event.pr is not None and event.pr.number == 123
    assert review_trigger(event).explicit_request
    assert event.raw["nyanpasu"]["trigger"] == "review_command"
    assert event.raw["nyanpasu"]["body_excerpt"] == body


@pytest.mark.parametrize(
    "body",
    [
        "ordinary comment",
        "/reviewer",
        "/reviews",
        "/review-all",
        "docs/review",
        "/review/file",
        "https://example.com/review",
    ],
)
def test_issue_comment_without_review_request_is_ignored(body: str) -> None:
    event = parse_github_event(
        "issue_comment",
        "delivery-1",
        issue_comment_payload(body),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.IGNORED


@pytest.mark.parametrize("action", ["created", "edited"])
@pytest.mark.parametrize("body", ["@review-bot please review", "/review"])
def test_issue_comment_by_agent_is_ignored_even_with_review_request(action: str, body: str) -> None:
    payload = issue_comment_payload(body)
    payload["action"] = action
    payload["comment"]["user"]["login"] = "REVIEW-BOT"
    event = parse_github_event("issue_comment", "self-comment", payload, agent_login="review-bot")
    assert event.action is ReviewAction.IGNORED


@pytest.mark.parametrize("is_pr,action", [(False, "created"), (True, "deleted")])
def test_review_command_ignores_ordinary_issues_and_deleted_comments(is_pr: bool, action: str) -> None:
    payload = issue_comment_payload("/review")
    payload["action"] = action
    if not is_pr:
        del payload["issue"]["pull_request"]

    event = parse_github_event("issue_comment", "delivery-1", payload, agent_login="review-bot")

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


@pytest.mark.parametrize("action", ["created", "edited", "updated"])
def test_pull_request_review_comment_command_triggers_review_without_reply(action: str) -> None:
    payload = review_comment_payload(body="/review", in_reply_to_id=None)
    payload["action"] = action

    event = parse_github_event("pull_request_review_comment", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.REVIEW
    assert review_trigger(event).explicit_request
    assert event.raw["nyanpasu"]["trigger"] == "review_command"


@pytest.mark.parametrize("body", ["ping", "/review"])
def test_pull_request_review_comment_by_agent_is_ignored(body: str) -> None:
    payload = review_comment_payload(body=body)
    payload["comment"]["user"] = {"login": "review-bot"}  # type: ignore[index]

    event = parse_github_event("pull_request_review_comment", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.IGNORED


@pytest.mark.parametrize(
    "body,trigger", [("@review-bot 看一下", "mentioned_pull_request_review"), ("/review", "review_command")]
)
def test_pull_request_review_body_request_triggers_review(body: str, trigger: str) -> None:
    event = parse_github_event(
        "pull_request_review",
        "delivery-1",
        pull_request_review_payload(body),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.REVIEW
    assert event.raw["nyanpasu"]["trigger"] == trigger
    assert review_trigger(event).explicit_request
    assert event.raw["nyanpasu"]["comment_url"].endswith("#pullrequestreview-30")


def test_pull_request_review_without_mention_is_ignored() -> None:
    event = parse_github_event(
        "pull_request_review",
        "delivery-1",
        pull_request_review_payload("ordinary review"),
        agent_login="review-bot",
    )

    assert event.action is ReviewAction.IGNORED


def test_pull_request_review_command_by_agent_is_ignored() -> None:
    payload = pull_request_review_payload("/review")
    payload["review"]["user"]["login"] = "REVIEW-BOT"

    event = parse_github_event("pull_request_review", "delivery-1", payload, agent_login="review-bot")

    assert event.action is ReviewAction.IGNORED
