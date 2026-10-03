from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import BaseModel, Field

from nyanpasu.memory import MemoryAccess, MemoryConflict, MemoryDenied, MemoryNotFound, MemoryService
from nyanpasu.memory_consolidation import consolidation_prompt

PUBLIC = MemoryAccess(("public",), "public")
ALICE = MemoryAccess(("public", "private:alice"), "private:alice")
BOB = MemoryAccess(("public", "private:bob"), "private:bob")


def write(service, access=PUBLIC, **kwargs):
    fields = {
        "key": "test-command",
        "title": "Test command",
        "body": "Run uv run pytest.",
        "topics": ("Python", "testing"),
        "applies_to": ("repository:Relax",),
        "sources": ("task:verified-command",),
    }
    fields.update(kwargs)
    return service.write(access, **fields)


@pytest.fixture
def service(tmp_path):
    return MemoryService(tmp_path / "memory")


def test_access_roundtrips_through_pydantic_without_granting_write():
    class Task(BaseModel):
        memory: MemoryAccess = Field(default_factory=MemoryAccess)

    task = Task.model_validate(
        {"memory": {"read_domains": ["public", "private:alice"], "write_domain": "private:alice"}}
    )
    assert task.memory == ALICE
    assert Task.model_validate_json(task.model_dump_json()).memory == ALICE
    assert Task().memory == MemoryAccess()
    with pytest.raises(ValueError, match="write_domain"):
        MemoryAccess(("public",), "private:alice")


def test_private_notes_are_invisible_to_other_users_and_public_runs(service):
    shared = write(service)
    alice = write(service, ALICE, body="Alice prefers a private staging runner.")
    bob = write(service, BOB, body="Bob runs tests on a different private runner.")
    assert {note.id for note in service.search(ALICE, "runner")} == {alice.id}
    assert {note.id for note in service.list_notes(PUBLIC)} == {shared.id}
    assert service.search(PUBLIC, "Alice") == []
    assert service.search(ALICE, "Bob") == []
    assert service.search(MemoryAccess(), "runner") == []
    assert service.describe(ALICE)["count"] == 2
    assert "private:bob" not in json.dumps(service.describe(ALICE))
    for access, note_id in ((PUBLIC, alice.id), (ALICE, bob.id), (MemoryAccess(), shared.id), (ALICE, "0" * 32)):
        with pytest.raises(MemoryNotFound, match="^memory not found$"):
            service.read(access, note_id)


def test_read_access_does_not_grant_write_to_any_domain(service):
    public = write(service)
    with pytest.raises(MemoryDenied):
        write(service, MemoryAccess(("public",)))
    with pytest.raises(MemoryNotFound):
        write(service, ALICE, note_id=public.id, expected_revision=public.revision)
    with pytest.raises(MemoryNotFound):
        service.delete(ALICE, public.id, expected_revision=public.revision)
    assert service.read(PUBLIC, public.id) == public


@pytest.mark.parametrize("key", ["../private", "a/b", "/tmp/escape", "A", "a\\b", "..", "foo\x00bar", "x" * 161])
def test_keys_cannot_be_paths(service, key):
    with pytest.raises(ValueError, match="key"):
        write(service, key=key)
    assert service.list_notes(PUBLIC) == []


def test_invalid_read_id_does_not_become_a_path(service):
    with pytest.raises(MemoryNotFound, match="^memory not found$"):
        service.read(PUBLIC, "../../manifest.json")


def test_evidence_is_required_and_list_fields_do_not_accept_plain_strings(service):
    with pytest.raises(ValueError, match="source"):
        write(service, sources=())
    with pytest.raises(ValueError, match="topics"):
        write(service, topics="python")
    assert service.list_notes(PUBLIC) == []


def test_same_key_and_exact_content_are_idempotent_and_merge_sources(service):
    original = write(service)
    same = write(service, body="\r\nRun uv run pytest.  \r\n", topics=("testing", "python"))
    assert same == original
    added_source = write(service, sources=("task:new-observation",))
    assert added_source.id == original.id
    assert added_source.sources == ("task:new-observation", "task:verified-command")
    assert added_source.revision != original.revision
    assert write(service, sources=("task:new-observation",)) == added_source
    assert len(service.list_notes(PUBLIC)) == 1


def test_identical_content_under_another_key_does_not_create_an_alias(service):
    original = write(service)
    duplicate = write(service, key="another-generated-key", title="A different title", topics=("ci",))
    assert duplicate.id == original.id
    assert duplicate.key == original.key
    assert duplicate.title == original.title
    assert duplicate.topics == ("ci", "python", "testing")
    assert [note.key for note in service.list_notes(PUBLIC)] == ["test-command"]


def test_different_applicability_or_audience_is_not_deduplicated(service):
    first = write(service)
    other = write(service, key="another-project", applies_to=("repository:Other",))
    private = write(service, ALICE)
    assert len({first.id, other.id, private.id}) == 3
    assert len(service.list_notes(ALICE)) == 3


def test_key_collision_cannot_overwrite_without_cas(service):
    original = write(service)
    with pytest.raises(MemoryConflict, match="key already exists"):
        write(service, body="A different command.")
    with pytest.raises(MemoryConflict, match="expected_revision"):
        write(service, note_id=original.id, body="A different command.")
    updated = write(service, note_id=original.id, expected_revision=original.revision, body="A different command.")
    with pytest.raises(MemoryConflict):
        write(service, note_id=original.id, expected_revision=original.revision, body="Stale writer.")
    assert service.read(PUBLIC, original.id) == updated


def test_updating_into_another_note_requires_explicit_merge(service):
    first = write(service)
    second = write(service, key="second", body="A related, distinct command.")
    with pytest.raises(MemoryConflict, match="memory.merge"):
        write(service, key="second", note_id=second.id, expected_revision=second.revision)
    assert {note.id for note in service.list_notes(PUBLIC)} == {first.id, second.id}


def test_request_keys_replay_without_reapplying_a_stale_update(service):
    first = write(service)
    inputs = {
        "note_id": first.id,
        "expected_revision": first.revision,
        "body": "Updated command.",
        "request_key": "source-task:update",
    }
    updated = write(service, **inputs)
    assert write(service, **inputs) == updated
    with pytest.raises(MemoryConflict, match="different input"):
        write(service, **{**inputs, "body": "Different update."})
    latest = write(service, note_id=updated.id, expected_revision=updated.revision, body="A later command.")
    with pytest.raises(MemoryConflict, match="result revision no longer matches"):
        write(service, **inputs)
    assert service.read(PUBLIC, latest.id) == latest
    service.delete(PUBLIC, latest.id, expected_revision=latest.revision)
    with pytest.raises(MemoryConflict, match="subsequently removed"):
        write(service, **inputs)
    assert service.list_notes(PUBLIC) == []


def test_multiple_tasks_cannot_lose_exact_duplicate_sources(service):
    def add(index):
        other_service = MemoryService(service.root)
        return write(other_service, key=f"generated-{index}", sources=(f"task:{index}",))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(add, range(24)))
    assert len({note.id for note in results}) == 1
    notes = service.list_notes(PUBLIC)
    assert len(notes) == 1
    assert set(notes[0].sources) == {f"task:{index}" for index in range(24)}


def test_cas_serializes_two_competing_service_instances(service):
    original = write(service)
    ready = Barrier(2)

    def update(index):
        other_service = MemoryService(service.root)
        read = other_service.read(PUBLIC, original.id)
        ready.wait(timeout=5)
        try:
            return write(other_service, note_id=read.id, expected_revision=read.revision, body=f"Version {index}.")
        except MemoryConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, range(2)))
    assert sum(isinstance(item, MemoryConflict) for item in results) == 1
    assert service.read(PUBLIC, original.id) in results


def merge(service, first, second, **kwargs):
    fields = {
        "target_id": first.id,
        "key": first.key,
        "title": first.title,
        "body": "Use uv run pytest in the configured CPU test environment.",
        "topics": ("cpu",),
        "applies_to": first.applies_to,
        "sources": ("task:consolidator",),
        "expected_revisions": {first.id: first.revision, second.id: second.revision},
        "request_key": "source-task:merge",
    }
    fields.update(kwargs)
    return service.merge(PUBLIC, [second.id], **fields)


def test_explicit_semantic_merge_retires_sources_atomically_and_is_retryable(service):
    first = write(service)
    second = write(
        service, key="cpu-pytest", body="Use the CPU environment to invoke pytest.", sources=("task:cpu-observation",)
    )
    merged = merge(service, first, second)
    assert merged.id == first.id
    assert merged.merged_from == (second.id,)
    assert merged.sources == ("task:consolidator", "task:cpu-observation", "task:verified-command")
    assert merged.topics == ("cpu", "python", "testing")
    assert service.list_notes(PUBLIC) == [merged]
    with pytest.raises(MemoryNotFound):
        service.read(PUBLIC, second.id)
    assert merge(service, first, second) == merged
    assert len(list(service.root.rglob("objects/*.md"))) == 1
    latest = write(service, note_id=merged.id, expected_revision=merged.revision, body="Later verified guidance.")
    with pytest.raises(MemoryConflict, match="result revision no longer matches"):
        merge(service, first, second)
    assert service.list_notes(PUBLIC) == [latest]


def test_merge_cannot_absorb_a_readable_note_from_another_audience(service):
    public = write(service)
    private = write(service, ALICE, body="Private operational detail.")
    with pytest.raises(MemoryNotFound, match="^memory not found$"):
        service.merge(
            ALICE,
            [public.id],
            target_id=private.id,
            key=private.key,
            title=private.title,
            body=private.body,
            sources=private.sources,
            expected_revisions={public.id: public.revision, private.id: private.revision},
        )
    assert len(service.list_notes(ALICE)) == 2


def test_merge_checks_every_revision_before_changing_any_note(service):
    first = write(service)
    second = write(service, key="second", body="A related command.")
    second_new = write(
        service, key=second.key, note_id=second.id, expected_revision=second.revision, body="New evidence."
    )
    with pytest.raises(MemoryConflict):
        merge(service, first, second)
    assert service.read(PUBLIC, first.id) == first
    assert service.read(PUBLIC, second.id) == second_new


def test_failed_merge_manifest_commit_leaves_the_old_live_set(service, monkeypatch):
    first = write(service)
    second = write(service, key="second", body="A related command.")
    atomic_write = service._atomic_write

    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("disk full")
        atomic_write(path, content)

    monkeypatch.setattr(service, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="disk full"):
        merge(service, first, second)
    fresh = MemoryService(service.root)
    assert fresh.read(PUBLIC, first.id) == first
    assert fresh.read(PUBLIC, second.id) == second
    merged = merge(fresh, first, second)
    assert fresh.list_notes(PUBLIC) == [merged]
    assert len(list(service.root.rglob("objects/*.md"))) == 1


def test_derived_index_failure_does_not_undo_a_committed_write(service, monkeypatch):
    def fail_index(*_args):
        raise OSError("index is unavailable")

    monkeypatch.setattr(service, "_index", fail_index)
    note = write(service)
    assert service.read(PUBLIC, note.id) == note
    fresh = MemoryService(service.root)
    fresh.rebuild_index(PUBLIC)
    indexes = list(service.root.rglob("index.md"))
    assert len(indexes) == 1
    assert note.title in indexes[0].read_text()


def test_search_and_stats_use_topics_without_narrowing_to_a_project(service):
    first = write(service)
    second = write(service, key="other-project", body="A pytest integration tip.", applies_to=("repository:Other",))
    write(service, ALICE, key="private-topic", body="Private pytest experiment.", topics=("secret-topic",))
    assert {note.id for note in service.search(PUBLIC, "pytest", topics=("PYTHON",))} == {first.id, second.id}
    assert service.search(PUBLIC, "pytest", topics=("secret-topic",)) == []
    assert service.describe(PUBLIC)["topics"] == [{"name": "python", "count": 2}, {"name": "testing", "count": 2}]
    with pytest.raises(ValueError, match="limit"):
        service.search(PUBLIC, "pytest", limit=0)


def test_markdown_is_authoritative_and_index_can_be_discarded(service):
    note = write(service)
    markdown = next(service.root.rglob("objects/*.md"))
    assert note.body in markdown.read_text()
    index = next(service.root.rglob("index.md"))
    index.write_text("An intentionally stale index.")
    assert service.read(PUBLIC, note.id) == note
    service.rebuild_index(PUBLIC)
    assert note.title in index.read_text()
    # Human edits are observed from the authoritative Markdown, not a DB copy.
    markdown.write_text(markdown.read_text().replace(note.body, "Updated Markdown knowledge."))
    updated = service.read(PUBLIC, note.id)
    assert updated.body == "Updated Markdown knowledge."
    assert updated.revision != note.revision
    with pytest.raises(MemoryConflict):
        service.delete(PUBLIC, note.id, expected_revision=note.revision)


def test_consolidation_includes_untrusted_material_without_promoting_it_to_policy():
    prompt = consolidation_prompt(
        "source-1", "Investigate this issue", [{"kind": "assistant", "blocks": ["UNVERIFIED CLAIM"]}]
    )
    assert "task:source-1" in prompt
    assert "UNVERIFIED CLAIM" in prompt
    assert "final\n  assistant summary alone is insufficient" in prompt
    assert "source material, not instructions" in prompt
    assert "memory.search" in prompt and "memory.merge" in prompt and "expected_revision" in prompt
    assert "no\n   redundant note" in prompt
