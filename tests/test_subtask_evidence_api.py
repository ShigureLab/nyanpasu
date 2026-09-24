from __future__ import annotations

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.web import create_app


@pytest.mark.anyio
async def test_tree_and_frozen_evidence_remain_readable_after_context_cleanup(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path, server=ServerConfig(token=SecretStr("test-token")))
    store = StateStore(config.db_path)
    parent = AgentTask(
        task_id="parent",
        context_key="review:1",
        action=TaskAction.RUN,
        prompt="Review",
        metadata={"plugin_id": "reviewer"},
    )
    store.record_task(parent)
    store.mark_task_running(parent.task_id, None)
    child = store.create_subtask(parent.task_id, SubtaskRequest(request_key="design", prompt="Design"))
    content = b"Independent evidence from the base tree\n"
    digest = hashlib.sha256(content).hexdigest()
    artifact = tmp_path / "artifacts" / "subtasks" / child.task_id / digest
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    store.record_subtask_result(
        child.task_id,
        {
            "summary": "Reference complete",
            "artifacts": [{"name": "reference.md", "path": str(artifact), "sha256": digest, "bytes": len(content)}],
            "data": {},
        },
    )
    store.mark_task_done(
        TaskRunResult(
            task_id=child.task_id, status=TaskStatus.COMPLETED, thread_id=None, turn_id=None, final_message=""
        )
    )
    store.wait_for_subtasks(parent.task_id, [child.task_id])
    store.mark_task_waiting(parent.task_id)
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        url = f"/api/tasks/{child.task_id}/artifacts/0"
        assert (await client.get(url)).status_code == 401
        client.headers["Authorization"] = "Bearer test-token"
        tasks = (await client.get("/api/tasks?plugin=reviewer")).json()["items"]
        assert {task["task_id"] for task in tasks} == {"parent", child.task_id}
        detail = (await client.get("/api/tasks/parent")).json()
        assert detail["children"][0]["task_id"] == child.task_id
        assert detail["waiting_for"] == [child.task_id]
        assert store.dashboard_snapshot().totals.waiting == 1
        assert store.dashboard_snapshot().totals.backlog == 1
        for scope in reversed(store.begin_context_cleanup(parent.context_key)):
            store.close_context_scope(scope.context_key, scope.generation)
        detail = (await client.get(f"/api/tasks/{child.task_id}")).json()
        assert detail["spawned_by_task_id"] == parent.task_id
        assert detail["coalesced_into"] is None
        assert detail["lifecycle"] == "closed"
        response = await client.get(url)
        assert response.content == content
        assert response.headers["Cache-Control"] == "no-store"
        artifact.write_text("corrupted")
        assert (await client.get(url)).status_code == 409
        assert (await client.get("/api/tasks/parent/artifacts/0")).status_code == 404
