from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction, TaskRunResult, TaskStatus, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu.transcript import TranscriptStore
from nyanpasu.web import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_search_reads_outside_preview_and_download_matches_fixed_export(tmp_path: Path):
    config = NyanpasuConfig(state_dir=tmp_path)
    state = StateStore(config.db_path)
    task = AgentTask(task_id="task", context_key="demo", action=TaskAction.RUN, prompt="original")
    state.record_task(task)
    journal = TranscriptStore(config.db_path)
    sid = journal.begin(task, None, "exec")
    text = ("开始🙂\n" * 10000) + "a unique NEEDLE" + ("\nend" * 10000)
    entry = journal.record(task.task_id, {"type": "nyanpasu.final", "text": text})
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        search = await client.get(f"/api/sessions/{sid}/search", params={"q": "NEEDLE"})
        assert search.status_code == 200
        hit = search.json()["items"][0]
        assert hit["entry_id"] == entry
        content = await client.get(
            f"/api/sessions/{sid}/content/{hit['content_ref']}", params={"offset": hit["offset"]}
        )
        assert content.json()["text"].startswith("NEEDLE")
        raw_search = await client.get(f"/api/sessions/{sid}/events", params={"q": "NEEDLE"})
        assert raw_search.json()["items"][0]["entry_id"] == entry
        downloaded = await client.get(f"/api/sessions/{sid}/content/{hit['content_ref']}?download=true")
        assert downloaded.text == text
        exported = await client.get(f"/api/sessions/{sid}/export?format=jsonl")
        records = [json.loads(line) for line in exported.text.splitlines()]
        assert records[0]["export"]["through_seq"]
        assert records[1]["event"]["text"] == text
        missing = await client.get("/api/sessions/missing")
        assert missing.status_code == 404
        invalid = await client.get(f"/api/sessions/{sid}/transcript", params={"limit": 10000})
        assert invalid.status_code == 422


def test_legacy_import_is_repeatable_and_does_not_invent_a_turn_for_coalesced_task(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    for task_id in ("old", "coalesced"):
        state.record_task(
            AgentTask(
                task_id=task_id,
                context_key="demo",
                action=TaskAction.RUN,
                prompt="old prompt",
                workspace=WorkspaceRef(key="demo", local_path=tmp_path),
            )
        )
    state.mark_task_done(
        TaskRunResult(
            task_id="old",
            status=TaskStatus.COMPLETED,
            thread_id="thread",
            turn_id=None,
            final_message="saved final",
            raw_events=[],
        )
    )
    state.mark_task_coalesced("coalesced", "old")
    journal = TranscriptStore(state.db_path)
    assert journal.import_legacy() == 1
    assert journal.import_legacy() == 0
    sessions = journal.reader.sessions()["items"]
    assert len(sessions) == 1
    assert sessions[0]["coverage"]["capture_gap"]
    entries = journal.reader.window(sessions[0]["session_id"])["entries"]
    assert any(block["preview"] == "saved final" for entry in entries for block in entry["blocks"])
    assert all(entry["turn_id"] is None for entry in entries)
