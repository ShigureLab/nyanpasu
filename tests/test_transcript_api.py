from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.web import create_app
from tests.session_source import MemorySessionSource, tool, turn

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_search_download_export_and_validation_use_native_content(tmp_path: Path):
    config = NyanpasuConfig(state_dir=tmp_path)
    state = StateStore(config.db_path)
    task = AgentTask(task_id="task", context_key="demo", action=TaskAction.RUN, prompt="original")
    state.record_task(task)
    state.bind_task_execution("task", "thread", "turn")
    text = ("开始🙂\n" * 10000) + "a unique NEEDLE" + ("\nend" * 10000)
    source = MemorySessionSource([turn("turn", tool("tool", text))])
    app = create_app(config, session_sources=lambda _: source)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        base = "/api/sessions/thread"
        search = await client.get(f"{base}/search", params={"q": "NEEDLE"})
        assert search.status_code == 200
        hit = search.json()["items"][0]
        assert hit["entry_id"] == "tool"
        content = await client.get(f"{base}/content/{hit['content_ref']}", params={"offset": hit["offset"]})
        assert content.json()["text"].startswith("NEEDLE")
        downloaded = await client.get(f"{base}/content/{hit['content_ref']}?download=true")
        assert downloaded.text == text
        exported = await client.get(f"{base}/export?format=jsonl")
        records = [json.loads(line) for line in exported.text.splitlines()]
        assert records[0] == {"turnId": "turn", "item": source.turns[0]["items"][0]}
        assert (await client.get("/api/tasks/task")).json()["entry_id"] == "tool"
        assert (await client.get("/api/sessions/missing")).status_code == 404
        assert (await client.get(f"{base}/transcript", params={"limit": 10000})).status_code == 422
        assert (await client.get(f"{base}/events")).status_code == 404
        assert (await client.get(f"{base}/content/invalid")).status_code == 400
        source.turns[0]["items"][0]["aggregatedOutput"] = "changed at source"
        assert (await client.get(f"{base}/content/{hit['content_ref']}")).status_code == 409
        assert (await client.get(f"{base}/search", params={"q": "NEEDLE"})).json()["items"] == []


@pytest.mark.anyio
async def test_unavailable_codex_does_not_hide_task_metadata_or_expose_other_threads(tmp_path: Path):
    config = NyanpasuConfig(state_dir=tmp_path)
    state = StateStore(config.db_path)
    state.record_task(AgentTask(task_id="failed", context_key="demo", action=TaskAction.RUN, prompt="request"))
    state.bind_task_execution("failed", "thread", "turn")
    state.mark_task_failed("failed", "backend process failed")

    class Unavailable(MemorySessionSource):
        async def read_thread(self, thread_id: str) -> dict:
            raise RuntimeError("Codex offline")

    app = create_app(config, session_sources=lambda _: Unavailable())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/sessions/thread/transcript")).status_code == 503
        assert (await client.get("/api/sessions/unrelated/transcript")).status_code == 404
        assert (await client.get("/api/sessions")).json()["items"][0]["state"] == "failed"
        assert (await client.get("/api/tasks/failed")).json()["error"] == "backend process failed"
