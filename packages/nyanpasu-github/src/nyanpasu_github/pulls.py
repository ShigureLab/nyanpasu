from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from nyanpasu_github.gh import gh_json
from nyanpasu_github.models import GitHubModel

GhJsonRunner = Callable[[list[str]], Any]


class PullRequestActivity(GitHubModel):
    kind: str
    id: str
    author: str
    body_excerpt: str = ""
    state: str = ""
    url: str = ""
    updated_at: str = ""


class PullRequestView(GitHubModel):
    repo: str
    number: int
    url: str
    state: str
    draft: bool
    base_ref: str
    head_ref: str
    head_sha: str
    review_decision: str = ""
    merge_state_status: str = ""
    updated_at: str = ""
    activities: tuple[PullRequestActivity, ...] = ()
    failing_checks: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.state.casefold() == "open"

    def follow_up_digest(self) -> str:
        payload = {
            "state": self.state,
            "draft": self.draft,
            "head_sha": self.head_sha,
            "review_decision": self.review_decision,
            "merge_state_status": self.merge_state_status,
            "activities": [activity.model_dump(mode="json") for activity in self.activities],
            "failing_checks": self.failing_checks,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def fetch_pull_request_view(
    repo: str,
    number: int,
    *,
    gh_runner: GhJsonRunner = gh_json,
) -> PullRequestView:
    fields = (
        "number,state,isDraft,url,baseRefName,headRefName,headRefOid,"
        "reviewDecision,mergeStateStatus,updatedAt,comments,reviews,statusCheckRollup"
    )
    data = gh_runner(["pr", "view", str(number), "--repo", repo, "--json", fields])
    if not isinstance(data, dict):
        raise ValueError("gh pr view response must be an object")
    return PullRequestView(
        repo=repo,
        number=int(data["number"]),
        url=str(data.get("url") or ""),
        state=str(data.get("state") or "").lower(),
        draft=bool(data.get("isDraft", False)),
        base_ref=str(data.get("baseRefName") or ""),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=str(data.get("headRefOid") or ""),
        review_decision=str(data.get("reviewDecision") or ""),
        merge_state_status=str(data.get("mergeStateStatus") or ""),
        updated_at=str(data.get("updatedAt") or ""),
        activities=_activities_from_pr_view(data),
        failing_checks=_failing_checks(data.get("statusCheckRollup")),
    )


def pull_request_number_from_url(url: str | None) -> int | None:
    if not url:
        return None
    match = re.search(r"/pull/(\d+)(?:\D|$)", url)
    if match is None:
        return None
    return int(match.group(1))


def _activities_from_pr_view(data: dict[str, Any]) -> tuple[PullRequestActivity, ...]:
    activities: list[PullRequestActivity] = []
    for comment in _as_dicts(data.get("comments")):
        activities.append(
            PullRequestActivity(
                kind="comment",
                id=str(comment.get("id") or comment.get("databaseId") or ""),
                author=_author_login(comment),
                body_excerpt=str(comment.get("body") or "").strip()[:1200],
                url=str(comment.get("url") or ""),
                updated_at=str(comment.get("updatedAt") or comment.get("createdAt") or ""),
            )
        )
    for review in _as_dicts(data.get("reviews")):
        activities.append(
            PullRequestActivity(
                kind="review",
                id=str(review.get("id") or review.get("databaseId") or ""),
                author=_author_login(review),
                body_excerpt=str(review.get("body") or "").strip()[:1200],
                state=str(review.get("state") or ""),
                url=str(review.get("url") or ""),
                updated_at=str(review.get("submittedAt") or review.get("updatedAt") or ""),
            )
        )
    activities.sort(key=lambda item: item.updated_at)
    return tuple(activities[-10:])


def _failing_checks(raw: Any) -> tuple[str, ...]:
    failures: list[str] = []
    for item in _as_dicts(raw):
        name = str(item.get("name") or item.get("context") or item.get("workflowName") or "").strip()
        if not name:
            continue
        conclusion = str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        if conclusion in {"FAILURE", "FAILED", "ERROR", "TIMED_OUT", "ACTION_REQUIRED"}:
            failures.append(name)
    return tuple(sorted(set(failures)))


def _author_login(item: dict[str, Any]) -> str:
    author = item.get("author") or item.get("user")
    if not isinstance(author, dict):
        return ""
    return str(author.get("login") or "")


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
