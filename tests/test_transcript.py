from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.transcript import TranscriptStore
from nyanpasu.transcript.models import TranscriptChanges, TranscriptWindow
from nyanpasu.transcript.queries import CursorError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    store = TranscriptStore(state.db_path)
    task = AgentTask(task_id="task-1", context_key="test:1", action=TaskAction.RUN, prompt="hello")
    state.record_task(task)
    session = store.begin(task, None, "app-server")
    return store, session


def item(item_id: str, output: str, *, completed: bool = False):
    return {
        "method": "item/completed" if completed else "item/started",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": item_id,
                "type": "commandExecution",
                "command": "echo same",
                "aggregatedOutput": output,
                "status": "completed" if completed else "inProgress",
            },
        },
    }


def test_concurrent_calls_keep_identity_and_terminal_snapshot_replaces_deltas(journal):
    store, session = journal
    first = store.record("task-1", item("a", ""))
    second = store.record("task-1", item("b", ""))
    store.record(
        "task-1",
        {
            "method": "item/commandExecution/outputDelta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "a", "delta": "partial"},
        },
    )
    store.record("task-1", item("b", "second", completed=True))
    store.record("task-1", item("a", "partial complete", completed=True))
    window = TranscriptWindow.model_validate(store.reader.window(session))
    assert [entry.entry_id for entry in window.entries] == [first, second]
    assert [entry.blocks[0].preview for entry in window.entries] == ["partial complete", "second"]
    assert window.entries[0].raw_event_count == 3


def test_changes_return_exact_versions_and_replay_is_idempotent(journal):
    store, session = journal
    window = store.reader.window(session)
    store.record("task-1", item("a", "before"), source_id="event-1")
    store.record("task-1", item("a", "after", completed=True), source_id="event-2")
    store.record("task-1", item("a", "after", completed=True), source_id="event-2")
    page = TranscriptChanges.model_validate(store.reader.window(session, after=window["change_cursor"], limit=1))
    assert page.changes[0].upserts[0].blocks[0].preview == "before"
    assert page.has_more
    next_page = store.reader.window(session, after=page.next_cursor)
    assert next_page["changes"][0]["upserts"][0]["blocks"][0]["preview"] == "after"
    assert not next_page["has_more"]
    assert len(store.reader.window(session)["entries"]) == 1


def test_long_unicode_content_is_bounded_and_old_versions_remain_readable(journal):
    store, session = journal
    original = "开始🙂\n" * 20000
    entry_id = store.record("task-1", item("a", original))
    before = store.reader.entry(session, entry_id)
    assert original.startswith(before["blocks"][0]["preview"])
    assert before["blocks"][0]["preview_truncated"]
    old_ref = before["blocks"][0]["content_ref"]
    store.record("task-1", item("a", "replacement", completed=True))
    assert len(json.dumps(store.reader.window(session)).encode()) < 256 * 1024
    output, offset = "", 0
    while True:
        page = store.reader.content(session, old_ref, offset=offset)
        assert len(page["text"].encode()) <= 65536
        output += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert output == original
    with pytest.raises(ValueError, match="UTF-8"):
        store.reader.content(session, old_ref, offset=1)
    with pytest.raises(KeyError):
        store.reader.content("another-session", old_ref)


def test_cursor_scope_and_history_do_not_confuse_changes(journal):
    store, session = journal
    for index in range(8):
        store.record("task-1", item(str(index), str(index)))
    latest = store.reader.window(session, limit=3)
    older = store.reader.window(session, before=latest["before_cursor"], limit=3)
    assert [e["source_item_id"] for e in older["entries"]] == ["2", "3", "4"]
    assert older["has_older"] and older["has_newer"]
    with pytest.raises(CursorError):
        store.reader.window(session, after=latest["before_cursor"])
    with pytest.raises(ValueError):
        store.reader.window(session, before=latest["before_cursor"], after=latest["change_cursor"])
    with store.db.connect(write=True) as conn:
        conn.execute("UPDATE transcript_sessions SET generation=? WHERE session_id=?", ("new", session))
    with pytest.raises(CursorError) as failure:
        store.reader.window(session, after=latest["change_cursor"])
    assert failure.value.status == 409


def test_replaced_thread_starts_new_session_without_rewriting_old_entries(journal):
    store, old_session = journal
    old_entry = store.record("task-1", item("first", "original"))
    state = StateStore(store.db.db_path)
    task = AgentTask(task_id="task-2", context_key="test:1", action=TaskAction.RUN, prompt="next")
    state.record_task(task)
    assert store.begin(task, "thread-1", "app-server") == old_session
    store.record(task.task_id, {"id": 10, "result": {"thread": {"id": "replacement"}}}, "server_response")
    store.record(task.task_id, {"type": "nyanpasu.final", "text": "new thread result"})
    sessions = store.reader.sessions()["items"]
    replacement = next(session for session in sessions if session["session_id"] != old_session)
    assert replacement["previous_session_id"] == old_session
    assert store.reader.session(old_session)["thread_id"] == "thread-1"
    assert store.reader.entry(old_session, old_entry)["blocks"][0]["preview"] == "original"
    assert store.reader.session(replacement["session_id"])["tasks"][0]["task_id"] == task.task_id


def test_around_entry_keeps_target_when_response_byte_budget_is_reached(journal):
    store, sid = journal
    state = StateStore(store.db.db_path)
    large = "evidence " * 1000
    store.record("task-1", {"type": "thread.started", "thread_id": "thread"})
    target = None
    for index in range(50):
        task = AgentTask(task_id=f"input-{index}", context_key="test:1", action=TaskAction.RUN, prompt=large)
        state.record_task(task)
        assert store.begin(task, "thread", "app-server") == sid
        entry = store.record(
            task.task_id,
            {"type": "nyanpasu.input", "prompt": large, "actual_prompt": large, "context": {"instructions": large}},
        )
        if index == 25:
            target = entry
    window = store.reader.window(sid, around=target)
    assert target in [entry["entry_id"] for entry in window["entries"]]
    assert len(json.dumps(window).encode()) < 256 * 1024


def test_tail_content_starts_at_valid_unicode_boundary(journal):
    store, sid = journal
    entry_id = store.record("task-1", {"type": "nyanpasu.final", "text": "你好🙂" * 20000})
    ref = store.reader.entry(sid, entry_id)["blocks"][0]["content_ref"]
    page = store.reader.content(sid, ref, tail=True)
    assert page["next_offset"] is None
    assert page["text"].endswith("你好🙂")
