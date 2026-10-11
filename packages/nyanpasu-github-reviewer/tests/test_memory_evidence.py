from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime

import pytest
from nyanpasu_github.gh import GitHubCommandError

from nyanpasu_github_reviewer.memory_evidence import fetch_review_feedback


def test_full_human_feedback_is_recovered_with_authorship_time_and_sources(monkeypatch):
    module = importlib.import_module("nyanpasu_github_reviewer.memory_evidence")
    base = {
        "user": {"login": "human-maintainer", "type": "User"},
        "body": "APPROVED, but remove acceptance-only framework coupling; preserve CLI compatibility.",
        "html_url": "https://github.com/Org/Repo/pull/258#pullrequestreview-1",
        "submitted_at": "2026-10-04T12:00:00Z",
    }
    calls = []

    def github(args, *, env):
        calls.append((args, env))
        if "/reviews?" in args[-1]:
            return [
                [
                    base,
                    {**base, "user": {"login": "review-agent", "type": "User"}},
                    {**base, "user": {"login": "external-bot", "type": "Bot"}},
                    {**base, "body": ""},
                    {**base, "submitted_at": "2026-10-08T12:00:00Z"},
                ],
                [{**base, "html_url": "https://github.com/Org/Repo/pull/258#pullrequestreview-1-page2"}],
            ]
        return [[{**base, "created_at": "2026-10-04T12:00:00Z", "updated_at": "2026-10-09T12:00:00Z"}]]

    monkeypatch.setattr(module, "gh_json", github)
    records = fetch_review_feedback(
        "Org/Repo",
        258,
        agent_login="review-agent",
        cutoff=datetime(2026, 10, 7, tzinfo=UTC).timestamp(),
        gh_env={"GH_TOKEN": "fixture-token"},
    )
    assert len(records) == 2  # Pagination retained; bot, empty, future and later-edited feedback excluded.
    for record in records:
        data = json.loads(record["blocks"][0])
        assert data["author"] == "human-maintainer"
        assert data["body"] == base["body"]
        assert data["created_at"] == base["submitted_at"]
        assert record["reference"] == data["url"]
        assert record["origin"] == "github"
    assert len(calls) == 3 and all("--paginate" in args and "--slurp" in args for args, _ in calls)
    assert all(env == {"GH_TOKEN": "fixture-token"} for _, env in calls)


def test_feedback_fetch_failure_is_not_silently_treated_as_empty(monkeypatch):
    module = importlib.import_module("nyanpasu_github_reviewer.memory_evidence")

    def unavailable(*args, **kwargs):
        raise GitHubCommandError(["api"], 1, "", "API unavailable")

    monkeypatch.setattr(module, "gh_json", unavailable)
    with pytest.raises(GitHubCommandError):
        fetch_review_feedback("Org/Repo", 258, agent_login="agent", cutoff=1, gh_env=None)
