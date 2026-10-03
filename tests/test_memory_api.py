from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.memory import MemoryAccess, MemoryService
from nyanpasu.models import AgentTask, TaskAction, WorkspaceRef
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
@pytest.mark.parametrize("with_workspace", [False, True], ids=["no-workspace", "workspace"])
async def test_operator_memory_api_lists_all_domains_and_can_narrow_to_task(tmp_path, with_workspace):
    config = NyanpasuConfig(state_dir=tmp_path, server=ServerConfig(token=SecretStr("operator-token")))
    memory = MemoryService(config.memory_dir)
    public = publish_source(memory, "public", "public-guidance", topics=("tests",))
    own = publish_source(memory, "private:alice", "alice-guidance", topics=("alice-topic",))
    other = publish_source(memory, "private:bob", "bob-guidance", topics=("bob-topic",))
    public_navigation = publish_navigation(memory, "public")
    own_navigation = publish_navigation(memory, "private:alice", body="ALICE_PRIVATE_NAVIGATION")
    other_navigation = publish_navigation(memory, "private:bob", body="BOB_PRIVATE_NAVIGATION")
    store = StateStore(config.db_path)
    workspace = None
    if with_workspace:
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        workspace = WorkspaceRef(key="checkout", local_path=workspace_path)
    for task_id, access in [("private-task", MemoryAccess(("public", "private:alice"))), ("no-memory", MemoryAccess())]:
        store.record_task(
            AgentTask(
                task_id=task_id,
                context_key=task_id,
                action=TaskAction.RUN,
                prompt="Inspect source summaries",
                execution=config.resolve_execution(),
                memory=access,
                workspace=workspace,
            )
        )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(
        transport=ASGITransport(app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer operator-token"},
    ) as client:
        assert (await client.get("/api/memory", headers={"Authorization": ""})).status_code == 401
        assert (await client.get(f"/api/memory/{own.id}", headers={"Authorization": ""})).status_code == 401
        response = await client.get("/api/memory")
        page = response.json()
        assert {source["id"] for source in page["items"]} == {public.id, own.id, other.id}
        assert {domain["id"]: domain["count"] for domain in page["domains"]} == {
            "public": 1,
            "private:alice": 1,
            "private:bob": 1,
        }
        assert {topic["name"] for topic in page["topics"]} == {"tests", "alice-topic", "bob-topic"}
        assert page["navigation_count"] == 3
        assert {navigation["id"] for navigation in page["navigation"]} == {
            public_navigation.id,
            own_navigation.id,
            other_navigation.id,
        }
        assert all(not navigation["stale"] for navigation in page["navigation"])
        assert all(source["complete"] for source in page["items"])
        assert all({"body", "key", "applies_to", "merged_from"}.isdisjoint(source) for source in page["items"])
        for source in (public, own, other):
            detail = await client.get(f"/api/memory/{source.id}")
            assert detail.status_code == 200
            assert detail.json()["body"] == source.body
            assert detail.json()["sources"] == list(source.sources)
        # New corpus domains are discoverable even without any task rows for that audience.
        later = publish_source(memory, "private:carol", "carol-guidance", topics=("carol-topic",))
        found = (await client.get("/api/memory?q=carol&topic=carol-topic")).json()
        assert [source["id"] for source in found["items"]] == [later.id]
        assert (await client.get(f"/api/memory/{later.id}")).status_code == 200
        # User-controlled domain/owner values cannot widen a selected task's scope.
        authorized = await client.get("/api/memory?task_id=private-task&domain=private:bob&owner=bob")
        assert authorized.status_code == 200
        assert {source["id"] for source in authorized.json()["items"]} == {public.id, own.id}
        assert {navigation["id"] for navigation in authorized.json()["navigation"]} == {
            public_navigation.id,
            own_navigation.id,
        }
        assert other_navigation.body not in authorized.text and "bob" not in authorized.text
        assert "carol" not in authorized.text
        detail = await client.get(f"/api/memory/{own.id}?task_id=private-task")
        assert detail.status_code == 200
        assert detail.json()["body"] == own.body
        assert detail.json()["sources"] == list(own.sources)
        assert {"key", "applies_to", "merged_from"}.isdisjoint(detail.json())
        assert (await client.get(f"/api/memory/{other.id}?task_id=private-task&domain=private:bob")).status_code == 404
        assert (await client.get("/api/memory?task_id=missing")).status_code == 404
        empty = (await client.get("/api/memory?task_id=no-memory")).json()
        assert empty["items"] == empty["navigation"] == empty["domains"] == empty["topics"] == []
        assert empty["count"] == empty["navigation_count"] == 0
        assert (await client.get(f"/api/memory/{public.id}?task_id=no-memory")).status_code == 404
        assert (await client.post("/api/memory", json={})).status_code == 405
        assert (await client.delete(f"/api/memory/{own.id}?task_id=private-task")).status_code == 405


@pytest.mark.parametrize("memory_enabled", [True, False])
@pytest.mark.parametrize("publication", ["source", "navigation"])
def test_tokenless_dashboard_rejects_private_corpus_without_task_history(tmp_path, memory_enabled, publication):
    config = NyanpasuConfig(state_dir=tmp_path, memory={"enabled": memory_enabled})
    memory = MemoryService(config.memory_dir)
    if publication == "source":
        publish_source(memory, "private:alice", "retained-private-source")
    else:
        publish_navigation(memory, "private:alice")
    assert StateStore(config.db_path).recent_tasks() == []
    with pytest.raises(ValueError, match="non-public history requires"):
        create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())


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
