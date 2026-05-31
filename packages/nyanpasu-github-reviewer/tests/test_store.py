from __future__ import annotations

from typing import TYPE_CHECKING

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
