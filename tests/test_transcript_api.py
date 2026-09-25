from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus
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
@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize(
    "status,lease_active",
    [("running", True), ("running", False), ("queued", False), ("failed", False), ("completed", False)],
)
async def test_session_state_comes_from_execution_not_coalesced_events(tmp_path: Path, backend, status, lease_active):
    config = NyanpasuConfig(state_dir=tmp_path)
    state = StateStore(config.db_path)
    parent = AgentTask(
        task_id="parent", context_key="demo", action=TaskAction.RUN, prompt="Actual review", coalesce_key="review"
    )
    previous = parent.model_copy(update={"task_id": "previous"})
    state.record_task(previous, default_backend=backend)
    state.bind_task_execution("previous", SESSION, "previous-turn", backend)
    state.mark_task_failed("previous", "Timed out")
    state.record_task(parent, default_backend=backend)
    child = parent.model_copy(update={"task_id": "child", "prompt": "Merged event"})
    assert state.enqueue_task(child, default_backend=backend, coalesce_since=0) == (True, "parent")
    assert state.task_status("child") == "completed"
    state.mark_task_running("parent", None)
    state.bind_task_execution("parent", SESSION, "current-turn", backend)
    if status == "queued":
        state.mark_task_interrupted("parent", "Awaiting recovery")
    elif status == "failed":
        state.mark_task_failed("parent", "Backend failed")
    elif status == "completed":
        state.mark_task_done(
            TaskRunResult(
                task_id="parent",
                status=TaskStatus.COMPLETED,
                backend=backend,
                thread_id=SESSION,
                turn_id="current-turn",
                final_message="done",
            )
        )
    if lease_active:
        assert state.try_acquire_context_lease("demo", owner_id="worker", task_id="parent", ttl_seconds=60)

    app = create_app(config, session_sources=lambda _: MemorySessionSource())
    session_id = SESSION if backend == "codex" else "claude:" + SESSION
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        detail = (await client.get("/api/sessions/" + session_id)).json()
        listed = (await client.get("/api/sessions", params={"state": status})).json()
        assert listed["total"] == 1
        for session in (detail, listed["items"][0]):
            assert session["state"] == status
            assert session["title"] == "Actual review"
            assert session["execution_uncertain"] == (status == "running" and not lease_active)
            assert session["task_count"] == 3
        assert [task["task_id"] for task in detail["tasks"]] == ["previous", "parent", "child"]
        if status != "completed":
            assert (await client.get("/api/sessions?state=completed")).json()["total"] == 0


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


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["codex", "claude"])
async def test_session_task_tree_preserves_ownership_evidence_and_history_without_native_reads(tmp_path: Path, backend):
    config = NyanpasuConfig(state_dir=tmp_path)
    store = StateStore(config.db_path)
    for index, name in enumerate(("earlier", "current"), start=1):
        store.record_task(
            AgentTask(task_id=name, context_key="pr", action=TaskAction.RUN, prompt=name), default_backend=backend
        )
        store.mark_task_running(name, None)
        store.bind_task_execution(name, "parent-session", name, backend)
        with store._connect() as conn:
            conn.execute("UPDATE task_runs SET created_at=? WHERE task_id=?", (index, name))
    old = store.create_subtask("earlier", SubtaskRequest(request_key="old", prompt="Old review", purpose="old"))
    store.mark_task_failed(old.task_id, "Old experiment failed")
    design = store.create_subtask("current", SubtaskRequest(request_key="design", prompt="Design", purpose="design"))
    store.bind_task_execution(design.task_id, "design-session", "design-turn", backend)
    store.record_subtask_result(
        design.task_id,
        {
            "summary": "Independent design verified",
            "artifacts": [{"name": "evidence.txt", "path": "/private/artifact", "sha256": "a" * 64, "bytes": 7}],
            "data": {},
        },
    )
    store.mark_task_done(
        TaskRunResult(
            task_id=design.task_id,
            status=TaskStatus.COMPLETED,
            backend=backend,
            thread_id="design-session",
            turn_id="design-turn",
            final_message="",
        )
    )
    audit = store.create_subtask("current", SubtaskRequest(request_key="audit", prompt="Audit", purpose="audit"))
    store.mark_task_running(audit.task_id, None)
    store.bind_task_execution(audit.task_id, "audit-session", "audit-turn", backend)
    experiment = store.create_subtask(audit.task_id, SubtaskRequest(request_key="experiment", prompt="Experiment"))
    store.wait_for_subtasks(audit.task_id, [experiment.task_id])
    store.mark_task_waiting(audit.task_id)
    store.wait_for_subtasks("current", [audit.task_id])
    store.mark_task_waiting("current")
    source = MemorySessionSource()
    app = create_app(config, session_sources=lambda _: source)
    prefix = "claude:" if backend == "claude" else ""
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        endpoint = f"/api/sessions/{prefix}parent-session/task-tree"
        response = await client.get(endpoint, params={"limit": 1})
        assert response.status_code == 200
        tree = response.json()
        assert tree["parent"] is None and tree["has_more"]
        root = tree["groups"][0]
        assert root["task_id"] == "current" and root["status"] == "waiting"
        children = {child["title"]: child for child in root["children"]}
        assert children["design"]["summary"] == "Independent design verified"
        assert children["design"]["session_id"] == prefix + "design-session"
        assert children["design"]["artifacts"][0]["name"] == "evidence.txt"
        assert "path" not in children["design"]["artifacts"][0]
        assert not children["design"]["waiting"]
        assert children["audit"]["waiting"]
        grandchild = children["audit"]["children"][0]
        assert grandchild["task_id"] == experiment.task_id and grandchild["waiting"]
        assert grandchild["status"] == "queued" and grandchild["session_id"] is None
        earlier = (await client.get(endpoint, params={"offset": 1, "limit": 1})).json()
        assert not earlier["has_more"] and earlier["groups"][0]["task_id"] == "earlier"
        assert earlier["groups"][0]["children"][0]["error"] == "Old experiment failed"
        detail = (await client.get(f"/api/sessions/{prefix}design-session/task-tree")).json()
        assert detail["groups"] == []
        assert detail["parent"] == {"task_id": "current", "session_id": prefix + "parent-session", "title": "current"}
        assert (await client.get("/api/sessions/missing/task-tree")).status_code == 404
        assert source.calls == []  # Relationships remain available even when native history is offline.
        roots = (await client.get("/api/sessions?include_subtasks=false&limit=1")).json()
        assert roots["total"] == 1 and not roots["has_more"]
        assert roots["items"][0]["session_id"] == prefix + "parent-session"
        assert roots["items"][0]["task_count"] == 2
        assert (await client.get("/api/sessions")).json()["total"] == 3
