from __future__ import annotations

from types import SimpleNamespace

import pytest
from nyanpasu_github.models import PullRequestRef

from nyanpasu_github_reviewer.ci import CIFailure, CISnapshot
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestSnapshot, RepoSettings
from nyanpasu_github_reviewer.poller import GitHubEventsPoller
from nyanpasu_github_reviewer.store import GitHubReviewerStore


def _raw_pr(head="head"):
    return {
        "number": 1,
        "node_id": "PR_1",
        "html_url": "https://github.com/base/repo/pull/1",
        "state": "open",
        "draft": False,
        "base": {"ref": "main", "sha": "base"},
        "head": {"ref": "feature", "sha": head, "repo": {"full_name": "fork/repo"}},
        "title": "test",
        "body": "",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }


def _snapshot(*, failed=True, head="head", attempt=1):
    return CISnapshot(
        head_sha=head,
        failures=(
            CIFailure(
                id="check:10",
                kind="check",
                name="tests",
                conclusion="failure",
                url="https://github.com/base/repo/actions/runs/50/job/10",
                tested_sha=head,
                run_id=50,
                run_attempt=attempt,
            ),
        )
        if failed
        else (),
    )


class Agent:
    def __init__(self):
        self.events = []
        self.unavailable = False

    async def submit(self, event):
        if self.unavailable:
            raise RuntimeError("temporary admission failure")
        self.events.append(event)
        return {"accepted": True}

    async def run_now(self, event):
        return await self.submit(event)


def _poller(tmp_path, *, observed=None, tracked=True):
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    raw = _raw_pr()
    if tracked:
        store.upsert_pr_snapshot(
            PullRequestSnapshot(
                **PullRequestRef.from_github("base/repo", raw).model_dump(),
                node_id="PR_1",
                head_repo="fork/repo",
                title_hash="title",
                body_hash="body",
                created_at=raw["created_at"],
                updated_at=raw["updated_at"],
            )
        )
    agent = Agent()
    state = {"snapshot": observed or _snapshot(), "raw": raw}

    def fetch(*args):
        result = state["snapshot"]
        if isinstance(result, Exception):
            raise result
        return result

    poller = GitHubEventsPoller(
        GitHubReviewerConfig(repos={"base/repo": RepoSettings(local_path=tmp_path)}),
        store=store,
        agent=agent,
        list_repo_events=lambda *_: [],
        list_pull_requests=lambda *_: [state["raw"]],
        list_pull_request_timeline=lambda *_: [],
        get_pull_request=lambda *_: state["raw"],
        fetch_ci=fetch,
    )
    return poller, store, agent, state


@pytest.mark.anyio
async def test_cold_start_existing_failure_wakes_without_a_new_pr_event(tmp_path):
    poller, store, agent, _ = _poller(tmp_path, tracked=False)
    result = await poller.run_once()
    assert result.submitted == 1
    (event,) = agent.events
    assert event.github_event == "ci" and event.raw["nyanpasu"]["trigger"] == "ci_changed"
    assert store.get_ci_snapshot("base/repo", 1) == _snapshot()


@pytest.mark.anyio
async def test_explicit_baseline_remains_silent_until_normal_ci_polling(tmp_path):
    poller, store, agent, _ = _poller(tmp_path, tracked=False)
    await poller.run_once(force_baseline=True)
    assert not agent.events
    assert store.get_ci_snapshot("base/repo", 1) is None
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 1


@pytest.mark.anyio
async def test_ci_only_poll_sees_failure_and_recovery_without_pr_updated_at_change(tmp_path):
    poller, store, agent, state = _poller(tmp_path, observed=_snapshot(failed=False))
    poller.list_repo_events = lambda *_: pytest.fail("CI tick must not poll repository events")
    poller.list_pull_requests = lambda *_: pytest.fail("CI tick must not scan all PRs")
    await poller.run_once(ci_only=True)
    assert not agent.events
    state["snapshot"] = _snapshot()
    await poller.run_once(ci_only=True)
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 1
    state["snapshot"] = _snapshot(failed=False)
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 2
    assert not store.get_ci_snapshot("base/repo", 1).failures
    state["snapshot"] = _snapshot()
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 3
    assert len({item.delivery_id for item in agent.events}) == 3


@pytest.mark.anyio
async def test_poll_read_error_preserves_failure_and_does_not_signal_recovery(tmp_path):
    poller, store, agent, state = _poller(tmp_path)
    await poller.run_once(ci_only=True)
    state["snapshot"] = RuntimeError("API unavailable")
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 1
    assert store.get_ci_snapshot("base/repo", 1) == _snapshot()


@pytest.mark.anyio
async def test_new_head_and_same_head_new_attempt_trigger_current_state_refresh(tmp_path):
    poller, store, agent, state = _poller(tmp_path)
    await poller.run_once(ci_only=True)
    state["snapshot"] = _snapshot(attempt=2)
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 2
    state["raw"] = _raw_pr("new-head")
    state["snapshot"] = _snapshot(failed=False, head="new-head")
    await poller.run_once(ci_only=True)
    assert len(agent.events) == 3
    assert agent.events[-1].pr.head_sha == "new-head"
    assert store.get_ci_snapshot("base/repo", 1).head_sha == "new-head"


@pytest.mark.anyio
async def test_ci_dispatch_failure_is_retried_after_restart_with_the_same_event(tmp_path):
    poller, store, agent, _ = _poller(tmp_path)
    agent.unavailable = True
    await poller.run_once(ci_only=True)
    (pending,) = store.pending_events(repo="base/repo")
    replacement, _, recovered_agent, _ = _poller(tmp_path)
    await replacement.run_once(ci_only=True)
    assert [item.delivery_id for item in recovered_agent.events] == [pending.delivery_id]


@pytest.mark.anyio
async def test_ci_interval_does_not_accelerate_repository_discovery(tmp_path, monkeypatch):
    poller, _, _, _ = _poller(tmp_path)
    now = [0.0]
    calls = []

    async def run_once(*args, **kwargs):
        calls.append((now[0], kwargs["ci_only"], kwargs["include_ci"]))

    async def sleep(seconds):
        now[0] += seconds
        if now[0] >= 720:
            raise RuntimeError("end clock")

    monkeypatch.setattr(poller, "run_once", run_once)
    monkeypatch.setattr("nyanpasu_github_reviewer.poller.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("nyanpasu_github_reviewer.poller.asyncio.sleep", sleep)
    with pytest.raises(RuntimeError, match="end clock"):
        await poller.run_forever()
    assert calls == [
        (0, False, True),
        (120, True, True),
        (240, True, True),
        (360, True, True),
        (480, True, True),
        (600, False, True),
    ]


def test_ci_observation_and_wakeup_commit_together(tmp_path, monkeypatch):
    _, store, _, _ = _poller(tmp_path)

    def fail(*args):
        raise RuntimeError("journal insert failed")

    monkeypatch.setattr("nyanpasu_github_reviewer.store._insert_event", fail)
    with pytest.raises(RuntimeError, match="journal insert failed"):
        store.record_ci_snapshot(PullRequestRef.from_github("base/repo", _raw_pr()), _snapshot())
    assert store.get_ci_snapshot("base/repo", 1) is None
