from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.transcript.models import TranscriptChanges, TranscriptWindow
from nyanpasu.transcript.queries import CursorError, TranscriptReader
from nyanpasu.transcript.source import RecordNotFound
from tests.session_source import MemorySessionSource, tool, turn

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def history(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    state.record_task(
        AgentTask(task_id="task", context_key="test:1", action=TaskAction.RUN, prompt="scheduler request")
    )
    state.bind_task_execution("task", "thread", "turn-1")
    source = MemorySessionSource([turn("turn-1", tool("a", "original"))])
    return state, source, TranscriptReader(state.db_path, lambda _: source)


@pytest.mark.anyio
async def test_sessions_group_native_threads_and_read_every_source_page(history):
    state, source, reader = history
    for index in range(2, 5):
        task_id = f"task-{index}"
        state.record_task(AgentTask(task_id=task_id, context_key="test:1", action=TaskAction.RUN, prompt="next"))
        state.bind_task_execution(task_id, "thread", f"turn-{index}")
        source.turns.append(turn(f"turn-{index}", tool(f"item-{index}", str(index))))
    assert (await reader.sessions())["total"] == 1
    assert (await reader.session("thread"))["task_count"] == 4
    window = TranscriptWindow.model_validate(await reader.window("thread"))
    assert window.session_id == "thread"
    assert len(window.entries) == 4
    assert window.entries[-1].task_id == "task-4"
    assert source.calls[-1] == ("turns", "thread", "2")
    with reader.connect() as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'transcript_%'").fetchall()
        assert "result_json" not in {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
    calls = len(source.calls)
    with pytest.raises(RecordNotFound):
        await reader.window("unrelated-private-thread")
    assert len(source.calls) == calls


@pytest.mark.anyio
async def test_changes_replay_current_source_and_include_previous_turn_completion(history):
    _, source, reader = history
    first = await reader.window("thread")
    assert not (await reader.window("thread", after=first["change_cursor"]))["changes"]
    source.turns[0]["items"][0]["aggregatedOutput"] = "final output"
    source.turns.append(turn("turn-2", tool("b", "next")))
    page = TranscriptChanges.model_validate(await reader.window("thread", after=first["change_cursor"], limit=1))
    assert page.changes[0].upserts[0].blocks[0].preview == "final output"
    assert page.has_more
    replay = await reader.window("thread", after=first["change_cursor"], limit=1)
    assert replay["changes"][0]["upserts"][0]["revision_seq"] == page.changes[0].upserts[0].revision_seq
    following = await reader.window("thread", after=page.next_cursor, limit=1)
    assert following["changes"][0]["upserts"][0]["entry_id"] == "b"
    assert not following["has_more"]
    assert not (await reader.window("thread", after=following["next_cursor"]))["changes"]
    source.turns.clear()
    with pytest.raises(CursorError) as error:
        await reader.window("thread", after=following["next_cursor"])
    assert error.value.status == 409


@pytest.mark.anyio
async def test_long_unicode_content_reads_source_and_invalidates_old_refs(history):
    _, source, reader = history
    original = "开始🙂\n" * 20000
    source.turns[0]["items"][0]["aggregatedOutput"] = original
    entry = await reader.entry("thread", "a")
    block = entry["blocks"][0]
    assert original.startswith(block["preview"]) and block["preview_truncated"]
    output, offset = "", 0
    while True:
        page = await reader.content("thread", block["content_ref"], offset=offset)
        output += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert output == original
    assert (await reader.content("thread", block["content_ref"], tail=True))["text"].endswith("开始🙂\n")
    with pytest.raises(ValueError, match="UTF-8"):
        await reader.content("thread", block["content_ref"], offset=1)
    source.turns[0]["items"][0]["aggregatedOutput"] = "replacement"
    with pytest.raises(CursorError) as error:
        await reader.content("thread", block["content_ref"])
    assert error.value.status == 409
    assert (await reader.entry("thread", "a"))["blocks"][0]["preview"] == "replacement"


@pytest.mark.anyio
async def test_history_pagination_and_cursor_scopes(history):
    _, source, reader = history
    source.turns[0]["items"] = [tool(str(index), "output") for index in range(8)]
    latest = await reader.window("thread", limit=3)
    older = await reader.window("thread", before=latest["before_cursor"], limit=3)
    assert [entry["entry_id"] for entry in older["entries"]] == ["2", "3", "4"]
    assert older["has_older"] and older["has_newer"]
    newer = await reader.window("thread", after_window=older["after_window_cursor"], limit=3)
    assert [entry["entry_id"] for entry in newer["entries"]] == ["5", "6", "7"]
    for bad in (latest["before_cursor"], "not-a-cursor"):
        with pytest.raises(CursorError):
            await reader.window("thread", after=bad)
    with pytest.raises(ValueError):
        await reader.window("thread", before=latest["before_cursor"], after=latest["change_cursor"])


@pytest.mark.anyio
async def test_around_window_is_bounded_and_keeps_target(history):
    _, source, reader = history
    source.turns[0]["items"] = [tool(str(index), "large output " * 1000) for index in range(100)]
    window = await reader.window("thread", around="55", limit=100)
    assert "55" in [entry["entry_id"] for entry in window["entries"]]
    assert len(json.dumps(window).encode()) < 256 * 1024


@pytest.mark.anyio
async def test_source_redaction_and_unknown_items_remain_inspectable(history):
    _, source, reader = history
    source.turns[0]["items"] = [
        {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "actual /tmp/worktree"}]},
        {"id": "future", "type": "newCodexItem", "value": "ghp_" + "a" * 30},
    ]
    window = await reader.window("thread")
    assert window["entries"][0]["blocks"][0]["preview"] == "actual /tmp/worktree"
    assert "scheduler request" not in json.dumps(window)
    assert window["coverage"]["redacted"]
    exported = await reader.export("thread", "jsonl")
    assert "ghp_" not in exported and "newCodexItem" in exported


@pytest.mark.anyio
async def test_step_times_are_read_from_codex_and_late_timing_updates_are_visible(history, tmp_path: Path):
    _, source, reader = history
    path = tmp_path / "rollout.jsonl"
    source.metadata = {"path": str(path), "cwd": "/native/worktree", "model": "native-model"}
    initial = await reader.window("thread")
    assert initial["entries"][0]["started_at"] is None
    record = {
        "timestamp": "2026-09-14T00:00:02Z",
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": "thread",
            "turn_id": "turn-1",
            "item": {"id": "a"},
            "started_at_ms": 1789344000123,
            "completed_at_ms": 1789344002456,
        },
    }
    path.write_text(json.dumps(record) + '\n{"partial":')
    changes = await reader.window("thread", after=initial["change_cursor"])
    entry = changes["changes"][0]["upserts"][0]
    assert entry["started_at"] == "2026-09-14T00:00:00.123000+00:00"
    assert entry["completed_at"] == "2026-09-14T00:00:02.456000+00:00"
    assert entry["recorded_at"] != entry["observed_at"]
    detail = await reader.session("thread")
    assert detail["runtime"]["model"] == "native-model"
    assert detail["runtime"]["cwd"] == "/native/worktree"
    record["payload"]["thread_id"] = "other-thread"
    path.write_text(json.dumps(record) + "\n")
    assert (await reader.entry("thread", "a"))["started_at"] is None
