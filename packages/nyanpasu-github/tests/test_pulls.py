from __future__ import annotations

from nyanpasu_github.pulls import fetch_pull_request_view, pull_request_number_from_url


def test_pull_request_number_from_url() -> None:
    assert pull_request_number_from_url("https://github.com/owner/repo/pull/123") == 123
    assert pull_request_number_from_url("https://github.com/owner/repo/pull/123#discussion") == 123
    assert pull_request_number_from_url("https://github.com/owner/repo/issues/123") is None


def test_fetch_pull_request_view_builds_actionable_digest() -> None:
    def gh_runner(args: list[str]):
        assert args[:4] == ["pr", "view", "5", "--repo"]
        return {
            "number": 5,
            "state": "OPEN",
            "isDraft": False,
            "url": "https://github.com/owner/repo/pull/5",
            "baseRefName": "main",
            "headRefName": "nyanpasu/task",
            "headRefOid": "abc",
            "reviewDecision": "CHANGES_REQUESTED",
            "mergeStateStatus": "BLOCKED",
            "updatedAt": "2026-05-31T00:00:00Z",
            "comments": [
                {
                    "id": "IC_1",
                    "author": {"login": "maintainer"},
                    "body": "please update tests",
                    "updatedAt": "2026-05-31T00:01:00Z",
                    "url": "https://github.com/owner/repo/pull/5#issuecomment-1",
                }
            ],
            "reviews": [
                {
                    "id": "PRR_1",
                    "author": {"login": "reviewer"},
                    "body": "needs changes",
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-05-31T00:02:00Z",
                    "url": "https://github.com/owner/repo/pull/5#pullrequestreview-1",
                }
            ],
            "statusCheckRollup": [
                {"name": "unit", "conclusion": "SUCCESS"},
                {"name": "lint", "conclusion": "FAILURE"},
            ],
        }

    pr = fetch_pull_request_view("owner/repo", 5, gh_runner=gh_runner)

    assert pr.is_open
    assert pr.failing_checks == ("lint",)
    assert [activity.kind for activity in pr.activities] == ["comment", "review"]
    assert pr.follow_up_digest() == pr.model_copy().follow_up_digest()
