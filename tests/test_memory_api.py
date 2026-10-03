from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.memory import MemoryAccess, MemoryService
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.plugins import PluginRegistry
from nyanpasu.store import StateStore
from nyanpasu.web import create_app
from tests.test_web import FakeAgent


def publish_source(service, domain, task_id, *, topics=(), body=None):
    return service.checkpoint_source(
        MemoryAccess((domain,), domain),
        task_id,
        input_digest=f"evidence:{task_id}",
        cursor=1,
        complete=True,
        title=task_id,
        body=body or f"Supported summary for {task_id}",
        topics=topics,
        sources=(f"tool:{task_id}:turn:result",),
    )


def publish_navigation(service, domain, *, body=None):
    access = MemoryAccess((domain,), domain)
    snapshot = service.snapshot_domain(access)
    return service.publish_navigation(
        access,
        body=body or f"Navigation for {domain}",
        source_ids=list(snapshot.source_revisions),
        source_revisions=snapshot.source_revisions,
        input_digest=snapshot.input_digest,
        expected_revision=None,
    )


@pytest.mark.anyio
async def test_memory_api_scopes_sources_and_navigation_to_persisted_task_capability(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path, server=ServerConfig(token=SecretStr("operator-token")))
    memory = MemoryService(config.memory_dir)
    public = publish_source(memory, "public", "public-guidance", topics=("tests",))
    own = publish_source(memory, "private:alice", "alice-guidance", topics=("alice-topic",))
    other = publish_source(memory, "private:bob", "bob-guidance", topics=("bob-topic",))
    public_navigation = publish_navigation(memory, "public")
    own_navigation = publish_navigation(memory, "private:alice", body="ALICE_PRIVATE_NAVIGATION")
    other_navigation = publish_navigation(memory, "private:bob", body="BOB_PRIVATE_NAVIGATION")
    store = StateStore(config.db_path)
    for task_id, access in [("private-task", MemoryAccess(("public", "private:alice"))), ("no-memory", MemoryAccess())]:
        store.record_task(
            AgentTask(
                task_id=task_id,
                context_key=task_id,
                action=TaskAction.RUN,
                prompt="Inspect source summaries",
                execution=config.resolve_execution(),
                memory=access,
            )
        )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test", headers={"Authorization": "Bearer operator-token"}
    ) as client:
        assert (await client.get("/api/memory", headers={"Authorization": ""})).status_code == 401
        # User-controlled domain/owner values cannot grant access to either document type.
        response = await client.get("/api/memory?domain=private:alice&owner=alice")
        page = response.json()
        assert [source["id"] for source in page["items"]] == [public.id]
        assert page["domains"] == [{"id": "public", "count": 1}]
        assert page["topics"] == [{"name": "tests", "count": 1}]
        assert page["navigation_count"] == 1
        assert [navigation["id"] for navigation in page["navigation"]] == [public_navigation.id]
        assert page["navigation"][0]["stale"] is False
        assert page["items"][0]["task_id"] == "public-guidance"
        assert page["items"][0]["complete"] is True
        assert {"body", "key", "applies_to", "merged_from"}.isdisjoint(page["items"][0])
        assert "alice" not in response.text and "bob" not in response.text
        for source in (own, other):
            assert (await client.get(f"/api/memory/{source.id}?domain={source.domain}")).status_code == 404
        authorized = await client.get("/api/memory?task_id=private-task")
        assert {source["id"] for source in authorized.json()["items"]} == {public.id, own.id}
        assert {navigation["id"] for navigation in authorized.json()["navigation"]} == {
            public_navigation.id,
            own_navigation.id,
        }
        assert other_navigation.body not in authorized.text and "bob" not in authorized.text
        detail = await client.get(f"/api/memory/{own.id}?task_id=private-task")
        assert detail.status_code == 200
        assert detail.json()["body"] == own.body
        assert detail.json()["sources"] == list(own.sources)
        assert {"key", "applies_to", "merged_from"}.isdisjoint(detail.json())
        assert (await client.get(f"/api/memory/{other.id}?task_id=private-task")).status_code == 404
        assert (await client.get("/api/memory?task_id=missing")).status_code == 404
        empty = (await client.get("/api/memory?task_id=no-memory")).json()
        assert empty["items"] == empty["navigation"] == empty["domains"] == empty["topics"] == []
        assert empty["count"] == empty["navigation_count"] == 0
        assert (await client.get(f"/api/memory/{public.id}?task_id=no-memory")).status_code == 404
        assert (await client.post("/api/memory", json={})).status_code == 405
        assert (await client.delete(f"/api/memory/{own.id}?task_id=private-task")).status_code == 405


@pytest.mark.anyio
async def test_memory_api_publishes_complete_sources_and_reports_stale_navigation(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path)
    memory = MemoryService(config.memory_dir)
    access = MemoryAccess(("public",), "public")
    first = publish_source(
        memory, "public", "python-tests", topics=("python", "tests"), body="Use shared pytest fixtures."
    )
    publish_source(memory, "public", "runtime-checks", topics=("runtime",))
    publish_navigation(memory, "public", body=f"[Python tests](memory:{first.id})")
    pending = memory.checkpoint_source(
        access,
        "not-yet-published",
        input_digest="pending-evidence",
        cursor=1,
        complete=False,
        title="Unpublished material",
        body="UNPUBLISHED_CHECKPOINT_CONTENT",
        topics=("unpublished-topic",),
        sources=("tool:pending:result",),
    )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        result = (await client.get("/api/memory?q=fixture&topic=python")).json()
        assert [source["id"] for source in result["items"]] == [first.id]
        assert result["items"][0]["revision"] == first.revision
        assert result["navigation"][0]["stale"] is False
        assert (await client.get("/api/memory?limit=1")).json()["has_more"] is True
        assert (await client.get("/api/memory?topic=not-present")).json()["items"] == []
        page = await client.get("/api/memory")
        assert "UNPUBLISHED" not in page.text and "unpublished-topic" not in page.text
        assert (await client.get(f"/api/memory/{pending.id}")).status_code == 404
        replacing = memory.checkpoint_source(
            access,
            "python-tests",
            input_digest="updated-evidence",
            cursor=1,
            complete=False,
            title="Python tests",
            body="UNPUBLISHED_REPLACEMENT",
            topics=("python",),
            sources=("tool:updated:result",),
            expected_revision=first.revision,
        )
        retained = (await client.get(f"/api/memory/{first.id}")).json()
        assert retained["body"] == first.body and retained["revision"] == first.revision
        assert (await client.get("/api/memory")).json()["navigation"][0]["stale"] is False
        completed = memory.checkpoint_source(
            access,
            "python-tests",
            input_digest="updated-evidence",
            cursor=2,
            complete=True,
            title="Python tests",
            body="Use shared pytest fixtures after the update.",
            topics=("python", "tests"),
            sources=("tool:updated:result",),
            expected_revision=replacing.revision,
        )
        detail = (await client.get(f"/api/memory/{first.id}")).json()
        assert detail["id"] == first.id == completed.id
        assert detail["body"] == completed.body and detail["revision"] == completed.revision
        assert (await client.get("/api/memory")).json()["navigation"][0]["stale"] is True


@pytest.mark.anyio
async def test_disabled_memory_hides_existing_sources_and_navigation(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path, memory={"enabled": False})
    memory = MemoryService(config.memory_dir)
    source = publish_source(memory, "public", "previous-source")
    publish_navigation(memory, "public")
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        data = (await client.get("/api/memory")).json()
        assert data["enabled"] is False
        assert data["items"] == data["navigation"] == data["domains"] == data["topics"] == []
        assert data["count"] == data["navigation_count"] == 0
        assert (await client.get(f"/api/memory/{source.id}")).status_code == 404
