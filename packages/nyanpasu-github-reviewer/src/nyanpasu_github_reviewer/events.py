from __future__ import annotations

import re
from typing import Any

from nyanpasu_github_reviewer.models import PullRequestRef, ReviewAction, ReviewEvent


def _repo_full_name(payload: dict[str, Any]) -> str:
    repository = payload.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str):
        raise ValueError("payload.repository.full_name is required")
    return repository["full_name"]


def _pr_ref(payload: dict[str, Any]) -> PullRequestRef:
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("payload.pull_request is required")
    head = pr.get("head")
    base = pr.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise ValueError("payload.pull_request.head/base are required")
    return PullRequestRef(
        repo=_repo_full_name(payload),
        number=int(pr["number"]),
        url=str(pr.get("html_url") or pr.get("url") or ""),
        base_ref=str(base["ref"]),
        head_ref=str(head["ref"]),
        head_sha=str(head["sha"]),
        state=str(pr.get("state", "open")).lower(),
        draft=bool(pr.get("draft", False)),
    )


def _issue_pr_ref(payload: dict[str, Any]) -> PullRequestRef:
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise ValueError("payload.issue is required")
    return PullRequestRef(
        repo=_repo_full_name(payload),
        number=int(issue["number"]),
        url=str(issue.get("html_url") or issue.get("url") or ""),
        base_ref="",
        head_ref="",
        head_sha="",
        state=str(issue.get("state", "open")).lower(),
        draft=False,
    )


def _mentions_login(text: str, login: str | None) -> bool:
    if not login:
        return False
    pattern = rf"(?<![A-Za-z0-9-])@{re.escape(login)}(?![A-Za-z0-9-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _comment_context(payload: dict[str, Any], *, trigger: str, summary: str) -> dict[str, Any]:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        comment = {}
    body = str(comment.get("body") or "")
    user_raw = comment.get("user")
    user = user_raw if isinstance(user_raw, dict) else {}
    return _with_nyanpasu_context(
        payload,
        trigger=trigger,
        trigger_summary=summary,
        actor=str(user.get("login") or ""),
        comment_url=str(comment.get("html_url") or comment.get("url") or ""),
        comment_id=comment.get("id"),
        body_excerpt=body.strip()[:1200],
        path=str(comment.get("path") or ""),
        line=comment.get("line") or comment.get("original_line"),
        in_reply_to_id=comment.get("in_reply_to_id"),
        pull_request_review_id=comment.get("pull_request_review_id"),
    )


def _requested_reviewer_matches(payload: dict[str, Any], agent_login: str | None) -> bool:
    reviewer = payload.get("requested_reviewer")
    if not isinstance(reviewer, dict) or not agent_login:
        return False
    login = reviewer.get("login")
    return isinstance(login, str) and login.casefold() == agent_login.casefold()


def _comment_author(payload: dict[str, Any]) -> str:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return ""
    user = comment.get("user")
    if not isinstance(user, dict):
        return ""
    return str(user.get("login") or "")


def _review_context(payload: dict[str, Any], *, trigger: str, summary: str) -> dict[str, Any]:
    review = payload.get("review")
    if not isinstance(review, dict):
        review = {}
    body = str(review.get("body") or "")
    user = review.get("user")
    if not isinstance(user, dict):
        user = {}
    return _with_nyanpasu_context(
        payload,
        trigger=trigger,
        trigger_summary=summary,
        actor=str(user.get("login") or ""),
        comment_url=str(review.get("html_url") or ""),
        body_excerpt=body.strip()[:1200],
        pull_request_review_id=review.get("id"),
    )


def _with_nyanpasu_context(payload: dict[str, Any], **context: Any) -> dict[str, Any]:
    raw = dict(payload)
    existing = raw.get("nyanpasu")
    raw["nyanpasu"] = (existing if isinstance(existing, dict) else {}) | context
    return raw


def parse_github_event(
    github_event: str,
    delivery_id: str,
    payload: dict[str, Any],
    *,
    agent_login: str | None = None,
) -> ReviewEvent:
    action_name = str(payload.get("action", ""))
    if github_event == "pull_request":
        pr = _pr_ref(payload)
        if action_name in {"closed", "merged"} or pr.state == "closed":
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.CLEANUP,
                pr=pr,
                after_sha=None,
                raw=payload,
            )
        if action_name == "review_requested" and _requested_reviewer_matches(payload, agent_login) and not pr.draft:
            reviewer = payload.get("requested_reviewer")
            reviewer_login = reviewer.get("login") if isinstance(reviewer, dict) else agent_login
            raw = _with_nyanpasu_context(
                payload,
                trigger="review_requested",
                trigger_summary=f"`{reviewer_login}` was explicitly requested as a reviewer.",
                actor=str(payload.get("sender", {}).get("login", ""))
                if isinstance(payload.get("sender"), dict)
                else "",
            )
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.REVIEW,
                pr=pr,
                after_sha=pr.head_sha,
                raw=raw,
            )
        if action_name in {"opened", "reopened", "ready_for_review", "synchronize", "edited"} and not pr.draft:
            raw = _with_nyanpasu_context(
                payload,
                trigger=f"pull_request_{action_name}",
                trigger_summary=f"Pull request `{action_name}` event.",
            )
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.REVIEW,
                pr=pr,
                after_sha=pr.head_sha,
                raw=raw,
            )
        return ReviewEvent(
            delivery_id=delivery_id,
            github_event=github_event,
            action=ReviewAction.IGNORED,
            pr=pr,
            after_sha=None,
            raw=payload,
        )

    if github_event == "issue_comment":
        issue = payload.get("issue")
        comment = payload.get("comment")
        if not isinstance(issue, dict) or not isinstance(issue.get("pull_request"), dict):
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.IGNORED,
                pr=None,
                after_sha=None,
                raw=payload,
            )
        pr = _issue_pr_ref(payload)
        body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
        if action_name in {"created", "edited"} and _mentions_login(body, agent_login):
            raw = _comment_context(
                payload,
                trigger="mentioned_issue_comment",
                summary=f"A PR comment explicitly mentioned `@{agent_login}`.",
            )
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.REVIEW,
                pr=pr,
                after_sha=None,
                raw=raw,
            )
        return ReviewEvent(
            delivery_id=delivery_id,
            github_event=github_event,
            action=ReviewAction.IGNORED,
            pr=pr,
            after_sha=None,
            raw=payload,
        )

    if github_event == "pull_request_review_comment":
        pr = _pr_ref(payload)
        if action_name not in {"created", "edited", "updated"}:
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.IGNORED,
                pr=pr,
                after_sha=None,
                raw=payload,
            )
        author = _comment_author(payload)
        if agent_login and author.casefold() == agent_login.casefold():
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.IGNORED,
                pr=pr,
                after_sha=None,
                raw=payload,
            )
        comment = payload.get("comment")
        body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
        is_reply = isinstance(comment, dict) and comment.get("in_reply_to_id") is not None
        if is_reply or _mentions_login(body, agent_login):
            raw = _comment_context(
                payload,
                trigger="review_thread_comment",
                summary="A user replied in a PR review thread or mentioned the agent in a review comment.",
            )
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.REVIEW,
                pr=pr,
                after_sha=pr.head_sha,
                raw=raw,
            )
        return ReviewEvent(
            delivery_id=delivery_id,
            github_event=github_event,
            action=ReviewAction.IGNORED,
            pr=pr,
            after_sha=None,
            raw=payload,
        )

    if github_event == "pull_request_review":
        pr = _pr_ref(payload)
        review = payload.get("review")
        if action_name not in {"submitted", "edited", "created", "updated"} or not isinstance(review, dict):
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.IGNORED,
                pr=pr,
                after_sha=None,
                raw=payload,
            )
        author = review.get("user")
        author_login = str(author.get("login") or "") if isinstance(author, dict) else ""
        if agent_login and author_login.casefold() == agent_login.casefold():
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.IGNORED,
                pr=pr,
                after_sha=None,
                raw=payload,
            )
        body = str(review.get("body") or "")
        if _mentions_login(body, agent_login):
            raw = _review_context(
                payload,
                trigger="mentioned_pull_request_review",
                summary=f"A PR review body explicitly mentioned `@{agent_login}`.",
            )
            return ReviewEvent(
                delivery_id=delivery_id,
                github_event=github_event,
                action=ReviewAction.REVIEW,
                pr=pr,
                after_sha=pr.head_sha,
                raw=raw,
            )
        return ReviewEvent(
            delivery_id=delivery_id,
            github_event=github_event,
            action=ReviewAction.IGNORED,
            pr=pr,
            after_sha=None,
            raw=payload,
        )

    if github_event == "push":
        after_sha = payload.get("after")
        prs = payload.get("pull_requests")
        if isinstance(prs, list) and prs:
            first = prs[0]
            if isinstance(first, dict):
                number = first.get("number")
                if number is not None:
                    ref = str(payload.get("ref", "")).removeprefix("refs/heads/")
                    pr = PullRequestRef(
                        repo=_repo_full_name(payload),
                        number=int(number),
                        url=str(first.get("html_url") or first.get("url") or ""),
                        base_ref=str(first.get("base", {}).get("ref", ""))
                        if isinstance(first.get("base"), dict)
                        else "",
                        head_ref=ref,
                        head_sha=str(after_sha or ""),
                        state=str(first.get("state", "open")).lower(),
                        draft=bool(first.get("draft", False)),
                    )
                    raw = _with_nyanpasu_context(
                        payload,
                        trigger="push_pull_request",
                        trigger_summary="A push payload referenced this pull request.",
                    )
                    return ReviewEvent(
                        delivery_id=delivery_id,
                        github_event=github_event,
                        action=ReviewAction.REVIEW,
                        pr=pr,
                        after_sha=str(after_sha),
                        raw=raw,
                    )
        return ReviewEvent(
            delivery_id=delivery_id,
            github_event=github_event,
            action=ReviewAction.IGNORED,
            pr=None,
            after_sha=str(after_sha) if after_sha else None,
            raw=payload,
        )

    return ReviewEvent(
        delivery_id=delivery_id,
        github_event=github_event,
        action=ReviewAction.IGNORED,
        pr=None,
        after_sha=None,
        raw=payload,
    )
