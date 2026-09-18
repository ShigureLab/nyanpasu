from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.transcript.claude import ClaudeHistorySource
from nyanpasu.web import create_app
from tests.claude_source import SESSION, records, write_session
from tests.session_source import MemorySessionSource, tool, turn

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_sessions_sort_and_show_latest_native_activity_before_pagination(tmp_path: Path):
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    state = StateStore(config.db_path)
    for index, backend in enumerate(("claude", "codex"), start=1):
        state.record_task(AgentTask(task_id=backend, context_key=backend, action=TaskAction.RUN, prompt=backend))
        state.bind_task_execution(backend, SESSION, None, backend)
        with state._connect() as conn:
            conn.execute("UPDATE task_runs SET created_at=?,updated_at=? WHERE task_id=?", (index, index, backend))
    home = tmp_path / "claude"
    data = records()
    path = write_session(home, data)
    codex_time = datetime.fromisoformat("2026-09-14T00:00:07.250000+00:00").timestamp()
    codex = MemorySessionSource(metadata={"updatedAt": codex_time})
    claude = ClaudeHistorySource({"CLAUDE_CONFIG_DIR": str(home)})
    app = create_app(config, session_sources={"codex": codex, "claude": claude}.__getitem__)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:

        async def first_session():
            response = await client.get("/api/sessions?limit=1")
            assert response.status_code == 200
            page = response.json()
            assert page["total"] == 2 and page["has_more"]
            return page["items"][0]

        assert (await first_session())["session_id"] == SESSION
        # New content in the older session, without a scheduler or task status update.
        data.append(
            {**data[-1], "uuid": "new-message", "parentUuid": "claude-final", "timestamp": "2026-09-14T08:00:08+08:00"}
        )
        write_session(home, data)
        latest = await first_session()
        assert latest["session_id"] == "claude:" + SESSION
        assert latest["updated_at"] == "2026-09-14T00:00:08+00:00"
        detail = (await client.get("/api/sessions/" + latest["session_id"])).json()
        assert detail["updated_at"] == latest["updated_at"]
        assert (await client.get("/api/sessions?offset=1&limit=1")).json()["items"][0]["session_id"] == SESSION

        codex.metadata["updatedAt"] = codex_time + 1
        latest = await first_session()
        assert latest["session_id"] == SESSION
        assert latest["updated_at"] == "2026-09-14T00:00:08.250000+00:00"
        assert all(method == "read" for method, _, _ in codex.calls)

        # A later task transition still counts, and missing history keeps task metadata visible.
        state.mark_task_failed("claude", "interrupted")
        path.unlink()
        latest = await first_session()
        assert latest["session_id"] == "claude:" + SESSION
        assert latest["state"] == "failed"
        with state._connect() as conn:
            task_time = conn.execute("SELECT updated_at FROM task_runs WHERE task_id='claude'").fetchone()[0]
        assert datetime.fromisoformat(latest["updated_at"]).timestamp() == pytest.approx(task_time, abs=1e-6, rel=0)


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
