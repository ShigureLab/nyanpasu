from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.config import NyanpasuConfig
from nyanpasu.memory import MemoryAccess, MemoryService
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.plugins import PluginRegistry
from nyanpasu.store import StateStore
from nyanpasu.web import create_app
from tests.test_web import FakeAgent


def write_note(service, domain, key, *, topics=(), body=None):
    return service.write(
        MemoryAccess((domain,), domain),
        key=key,
        title=key,
        body=body or f"Evidence for {key}",
        topics=topics,
        applies_to=("repository:Relax",),
        sources=("task:fixture",),
    )


@pytest.mark.anyio
async def test_memory_api_uses_public_or_persisted_task_capability(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path)
    memory = MemoryService(config.memory_dir)
    public = write_note(memory, "public", "public-guidance", topics=("tests",))
    own = write_note(memory, "private:alice", "alice-guidance", topics=("alice-topic",))
    other = write_note(memory, "private:bob", "bob-guidance", topics=("bob-topic",))
    store = StateStore(config.db_path)
    for task_id, access in [("private-task", MemoryAccess(("public", "private:alice"))), ("no-memory", MemoryAccess())]:
        store.record_task(
            AgentTask(
                task_id=task_id,
                context_key=task_id,
                action=TaskAction.RUN,
                prompt="Inspect evidence",
                execution=config.resolve_execution(),
                memory=access,
            )
        )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        # Domain/owner input is not an authorization surface, even with a valid note ID.
        page = (await client.get("/api/memory?domain=private:alice&owner=alice")).json()
        assert [note["id"] for note in page["items"]] == [public.id]
        assert page["domains"] == [{"id": "public", "count": 1}]
        assert page["topics"] == [{"name": "tests", "count": 1}]
        assert "body" not in page["items"][0]
        for note in (own, other):
            assert (await client.get(f"/api/memory/{note.id}")).status_code == 404
        authorized = await client.get("/api/memory?task_id=private-task")
        assert {note["id"] for note in authorized.json()["items"]} == {public.id, own.id}
        assert "bob" not in authorized.text
        detail = await client.get(f"/api/memory/{own.id}?task_id=private-task")
        assert detail.json()["body"] == own.body
        assert (await client.get(f"/api/memory/{other.id}?task_id=private-task")).status_code == 404
        assert (await client.get("/api/memory?task_id=missing")).status_code == 404
        assert (await client.get("/api/memory?task_id=no-memory")).json()["items"] == []
        assert (await client.get(f"/api/memory/{public.id}?task_id=no-memory")).status_code == 404


@pytest.mark.anyio
async def test_memory_api_search_and_merge_show_only_live_notes_and_stable_revisions(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path)
    memory = MemoryService(config.memory_dir)
    access = MemoryAccess(("public",), "public")
    first = write_note(memory, "public", "python-tests", topics=("python", "tests"))
    duplicate = write_note(memory, "public", "python-tests", topics=("python", "tests"))
    assert duplicate.revision == first.revision
    second = write_note(memory, "public", "pytest-fixtures", topics=("tests",))
    write_note(memory, "public", "unrelated", topics=("runtime",))
    merged = memory.merge(
        access,
        (second.id,),
        target_id=first.id,
        key=first.key,
        title="Python testing guide",
        body="Use shared pytest fixtures.",
        topics=("python", "tests"),
        applies_to=first.applies_to,
        sources=("task:consolidation",),
        expected_revisions={first.id: first.revision, second.id: second.revision},
    )
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        result = (await client.get("/api/memory?q=fixture&topic=python")).json()
        assert [note["id"] for note in result["items"]] == [first.id]
        assert result["items"][0]["revision"] == merged.revision
        assert result["items"][0]["merged_from"] == [second.id]
        assert result["items"][0]["sources"] == ["task:consolidation", "task:fixture"]
        assert (await client.get(f"/api/memory/{second.id}")).status_code == 404
        assert (await client.get("/api/memory?limit=1")).json()["has_more"] is True
        assert (await client.get("/api/memory?topic=not-present")).json()["items"] == []


@pytest.mark.anyio
async def test_disabled_memory_does_not_expose_existing_notes(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path, memory={"enabled": False})
    note = write_note(MemoryService(config.memory_dir), "public", "previous-note")
    app = create_app(config, agent=FakeAgent(), plugin_registry=PluginRegistry())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        data = (await client.get("/api/memory")).json()
        assert data["enabled"] is False
        assert data["items"] == data["domains"] == data["topics"] == []
        assert (await client.get(f"/api/memory/{note.id}")).status_code == 404
