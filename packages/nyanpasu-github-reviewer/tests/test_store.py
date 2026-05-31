from __future__ import annotations

from typing import TYPE_CHECKING

from nyanpasu_github_reviewer.models import (
    GitHubEventJournalRecord,
    GitHubEventJournalStatus,
    PullRequestRef,
    PullRequestSnapshot,
    PullRequestTimelineCursor,
    ReviewAction,
    ReviewEvent,
)
from nyanpasu_github_reviewer.store import GitHubReviewerStore

if TYPE_CHECKING:
    from pathlib import Path


def test_poll_event_cursor_roundtrip(tmp_path: Path) -> None:
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")

    assert store.get_poll_event_cursor("ExampleOrg/ExampleRepo") is None

    first = store.upsert_poll_event_cursor(
        "ExampleOrg/ExampleRepo",
        last_event_created_at="2026-05-30T10:00:00Z",
        cursor_event_ids=("100", "101"),
    )
    assert first.repo == "ExampleOrg/ExampleRepo"
    assert first.last_event_created_at == "2026-05-30T10:00:00Z"
    assert first.cursor_event_ids == ("100", "101")

    loaded = store.get_poll_event_cursor("ExampleOrg/ExampleRepo")
    assert loaded == first

    second = store.upsert_poll_event_cursor(
        "ExampleOrg/ExampleRepo",
        last_event_created_at="2026-05-30T10:05:00Z",
        cursor_event_ids=("102",),
    )
    assert second.initialized_at == first.initialized_at
    assert second.updated_at >= first.updated_at
    assert store.get_poll_event_cursor("ExampleOrg/ExampleRepo") == second


def test_pr_updated_cursor_and_snapshot_roundtrip(tmp_path: Path) -> None:
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")

    cursor = store.upsert_pr_updated_cursor(
        "ExampleOrg/ExampleRepo",
        last_updated_at="2026-05-30T10:00:00Z",
        pr_node_ids=("PR_1",),
    )
    snapshot = PullRequestSnapshot(
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

    store.upsert_pr_snapshot(snapshot)

    assert store.get_pr_updated_cursor("ExampleOrg/ExampleRepo") == cursor
    assert store.get_pr_snapshot("ExampleOrg/ExampleRepo", 1) == snapshot


def test_pr_timeline_cursor_roundtrip(tmp_path: Path) -> None:
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")

    first = store.upsert_pr_timeline_cursor(
        "ExampleOrg/ExampleRepo",
        1,
        last_item_updated_at="2026-05-30T10:00:00Z",
        item_ids=("IC_1",),
    )
    second = store.upsert_pr_timeline_cursor(
        "ExampleOrg/ExampleRepo",
        1,
        last_item_updated_at="2026-05-30T10:05:00Z",
        item_ids=("IC_2", "RC_3"),
    )

    assert isinstance(first, PullRequestTimelineCursor)
    assert second.initialized_at == first.initialized_at
    assert second.updated_at >= first.updated_at
    assert store.get_pr_timeline_cursor("ExampleOrg/ExampleRepo", 1) == second


def test_event_journal_roundtrip_and_status_update(tmp_path: Path) -> None:
    store = GitHubReviewerStore(tmp_path / "state.sqlite3")
    pr = PullRequestRef(
        repo="ExampleOrg/ExampleRepo",
        number=1,
        url="https://github.com/ExampleOrg/ExampleRepo/pull/1",
        base_ref="main",
        head_ref="feature",
        head_sha="abc123",
        state="open",
        draft=False,
    )
    event = ReviewEvent(
        delivery_id="delivery-1",
        github_event="pull_request",
        action=ReviewAction.REVIEW,
        pr=pr,
        after_sha="abc123",
        raw={"action": "synchronize", "nyanpasu": {"trigger": "pull_request_synchronize"}},
    )
    record = GitHubEventJournalRecord(
        delivery_id="delivery-1",
        dedupe_key="pull_request:synchronize:ExampleOrg/ExampleRepo#1:abc123",
        source="pr_state_poll",
        repo="ExampleOrg/ExampleRepo",
        pr_number=1,
        github_event="pull_request",
        action=ReviewAction.REVIEW,
        event_created_at="2026-05-30T10:00:00Z",
        event=event,
        created_at=1.0,
        updated_at=1.0,
    )

    assert store.append_event(record) is True
    assert store.append_event(record) is False
    pending = store.pending_events(repo="ExampleOrg/ExampleRepo")
    assert pending == [record]

    store.mark_event_status("delivery-1", GitHubEventJournalStatus.COMPLETED, result_json='{"accepted": true}')

    assert store.pending_events(repo="ExampleOrg/ExampleRepo") == []
