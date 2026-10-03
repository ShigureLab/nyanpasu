from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import BaseModel, Field

from nyanpasu.memory import (
    NAVIGATION_MAX_CHARS,
    SOURCE_BODY_MAX_CHARS,
    MemoryAccess,
    MemoryConflict,
    MemoryDenied,
    MemoryNotFound,
    MemoryService,
)

PUBLIC = MemoryAccess(("public",), "public")
ALICE = MemoryAccess(("public", "private:alice"), "private:alice")
BOB = MemoryAccess(("public", "private:bob"), "private:bob")


def checkpoint(service, access=PUBLIC, task_id="verified-task", **overrides):
    fields = {
        "input_digest": "evidence-v1",
        "cursor": 1,
        "complete": True,
        "title": "Testing workflow",
        "body": "The repository passed its Python tests.",
        "topics": ("Python", "testing"),
        "sources": ("codex:session:turn:result",),
    }
    return service.checkpoint_source(access, task_id, **(fields | overrides))


def navigation(service, access=PUBLIC, snapshot=None, **overrides):
    snapshot = snapshot or service.snapshot_domain(access)
    fields = {
        "body": "\n".join(f"- [{source.title}](memory:{source.id})" for source in snapshot.sources),
        "source_ids": [source.id for source in snapshot.sources],
        "source_revisions": snapshot.source_revisions,
        "input_digest": snapshot.input_digest,
        "expected_revision": snapshot.navigation.revision if snapshot.navigation else None,
    }
    return service.publish_navigation(access, **(fields | overrides))


@pytest.fixture
def service(tmp_path):
    return MemoryService(tmp_path / "memory")


def test_access_roundtrips_through_pydantic_without_granting_a_contribution_domain():
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


def test_sources_and_navigation_are_invisible_outside_the_authorized_audience(service):
    shared = checkpoint(service)
    alice = checkpoint(service, ALICE, body="Alice uses a private staging runner.")
    bob = checkpoint(service, BOB, body="Bob uses his private runner.")
    navigation(service)
    navigation(service, ALICE)
    navigation(service, BOB)

    assert {source.id for source in service.search(ALICE, "runner")} == {alice.id}
    assert {source.id for source in service.list_sources(PUBLIC)} == {shared.id}
    assert service.search(PUBLIC, "Alice") == []
    assert service.search(ALICE, "Bob") == []
    assert service.search(MemoryAccess(), "runner") == []
    assert {item.domain for item in service.list_navigation(ALICE)} == {"public", "private:alice"}
    assert service.list_navigation(MemoryAccess()) == []
    assert service.describe(ALICE)["count"] == 2
    assert "private:bob" not in json.dumps(service.describe(ALICE))
    for access, source_id in (
        (PUBLIC, alice.id),
        (ALICE, bob.id),
        (MemoryAccess(), shared.id),
        (ALICE, "0" * 32),
        (PUBLIC, "../../manifest.json"),
    ):
        with pytest.raises(MemoryNotFound, match="^memory source not found$"):
            service.read(access, source_id)


def test_production_uses_only_the_contribution_domain_not_all_readable_domains(service):
    shared = checkpoint(service)
    assert service.source_state(ALICE, shared.task_id) is None
    assert service.snapshot_domain(ALICE).sources == ()
    private = checkpoint(service, ALICE)
    assert private.id != shared.id
    assert [source.id for source in service.snapshot_domain(ALICE).sources] == [private.id]
    with pytest.raises(ValueError, match="supplied source"):
        navigation(service, ALICE, source_ids=[shared.id])
    assert service.snapshot_domain(ALICE).navigation is None


def test_read_access_never_grants_background_writes_or_checkpoint_reads(service):
    checkpoint(service)
    reader = MemoryAccess(("public",))
    assert len(service.list_sources(reader)) == 1
    for operation in (
        lambda: checkpoint(service, reader),
        lambda: service.source_state(reader, "verified-task"),
        lambda: service.snapshot_domain(reader),
        lambda: service.publish_navigation(reader, body="", source_ids=[], source_revisions={}, input_digest="x"),
    ):
        with pytest.raises(MemoryDenied):
            operation()


def test_partial_extraction_is_durable_but_not_recalled_until_complete(service):
    partial = checkpoint(service, cursor=1, complete=False, body="First chunk result.")
    restarted = MemoryService(service.root)
    restored = restarted.source_state(PUBLIC, partial.task_id)
    assert restored == partial
    assert restored is not None and restored.cursor == 1 and not restored.complete
    assert restarted.list_sources(PUBLIC) == []
    assert restarted.snapshot_domain(PUBLIC).sources == ()
    with pytest.raises(MemoryNotFound):
        restarted.read(PUBLIC, partial.id)

    final = checkpoint(
        restarted,
        cursor=2,
        body="First chunk result. Second chunk result.",
        sources=(*partial.sources, "codex:session:turn:second-result"),
        expected_revision=partial.revision,
    )
    recalled = restarted.read(PUBLIC, final.id)
    assert recalled.id == partial.id
    assert recalled.complete
    assert recalled.body == "First chunk result. Second chunk result."
    assert set(recalled.sources) == {
        "task:verified-task",
        "codex:session:turn:result",
        "codex:session:turn:second-result",
    }
    assert "cursor" not in recalled.to_dict()


def test_source_replay_does_not_rewrite_files_or_advance_progress(service, monkeypatch):
    first = checkpoint(service, complete=False)

    def unexpected_write(*_):
        pytest.fail("a committed source checkpoint was written again")

    monkeypatch.setattr(service, "_atomic_write", unexpected_write)
    replay = checkpoint(service, body="\r\nThe repository passed its Python tests.  \r\n", complete=False)
    assert replay == first
    assert replay.cursor == 1


def test_checkpoint_cas_prevents_lost_or_out_of_order_chunks(service):
    first = checkpoint(service, complete=False)
    with pytest.raises(MemoryConflict, match="current revision"):
        checkpoint(service, cursor=2, body="A different worker's second chunk.")
    with pytest.raises(MemoryConflict, match="did not advance"):
        checkpoint(service, cursor=1, complete=False, body="Replaced first chunk.", expected_revision=first.revision)
    with pytest.raises(MemoryConflict, match="did not advance"):
        checkpoint(service, cursor=0, complete=False, expected_revision=first.revision)
    final = checkpoint(service, cursor=2, body="Both chunks.", expected_revision=first.revision)
    with pytest.raises(MemoryConflict, match="already complete"):
        checkpoint(service, cursor=3, body="Unexpected extra chunk.", expected_revision=final.revision)
    with pytest.raises(MemoryConflict):
        checkpoint(service, complete=False, expected_revision=first.revision)
    assert service.read(PUBLIC, final.id).body == "Both chunks."


def test_reextraction_keeps_previous_publication_until_new_input_is_complete(service):
    original = checkpoint(service)
    old_navigation = navigation(service)
    pending = checkpoint(
        service,
        input_digest="evidence-v2",
        complete=False,
        body="A newer partial account.",
        expected_revision=original.revision,
    )
    assert pending.id == original.id
    assert service.read(PUBLIC, original.id).revision == original.revision
    assert service.list_navigation(PUBLIC) == [old_navigation]
    updated = checkpoint(
        service,
        input_digest="evidence-v2",
        cursor=2,
        body="A complete newer account.",
        expected_revision=pending.revision,
    )
    assert updated.id == original.id
    assert service.read(PUBLIC, original.id).body == updated.body
    assert service.list_navigation(PUBLIC)[0].stale
    current = navigation(service)
    assert not current.stale
    assert current.input_digest == service.snapshot_domain(PUBLIC).input_digest


def test_empty_completed_account_is_a_persistent_receipt_and_removes_old_recall(service):
    original = checkpoint(service)
    navigation(service)
    empty = checkpoint(service, input_digest="evidence-v2", body="", expected_revision=original.revision)
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, original.task_id) == empty
    assert empty.complete and empty.body == ""
    assert restarted.list_sources(PUBLIC) == []
    assert restarted.describe(PUBLIC)["count"] == 0
    assert restarted.list_navigation(PUBLIC)[0].stale
    assert checkpoint(restarted, input_digest="evidence-v2", body="") == empty
    with pytest.raises(MemoryNotFound):
        restarted.read(PUBLIC, original.id)
    cleared = navigation(restarted, body="No retained sources.")
    assert cleared.sources == ()
    assert not restarted.list_navigation(PUBLIC)[0].stale


def test_different_source_tasks_preserve_separate_provenance_even_with_identical_summaries(service):
    first = checkpoint(service, task_id="task-a")
    second = checkpoint(service, task_id="task-b")
    assert first.id != second.id
    assert {source.task_id for source in service.search(PUBLIC, "tests")} == {"task-a", "task-b"}
    assert "task:task-a" in first.sources
    assert "task:task-b" not in first.sources


def test_source_identity_is_stable_and_does_not_become_a_filesystem_path(service):
    source = checkpoint(service, task_id="../../elsewhere/源任务")
    assert source.task_id == "../../elsewhere/源任务"
    assert len(source.id) == 32
    assert MemoryService(service.root).source_state(PUBLIC, source.task_id) == source
    assert not (service.root.parent / "elsewhere").exists()


def test_topic_filter_and_weighted_retrieval_are_scoped_and_search_source_content(service):
    body_match = checkpoint(service, task_id="a", title="General result", body="A Python result", topics=("runtime",))
    title_match = checkpoint(
        service, task_id="b", title="Python workflow", body="Verified workflow", topics=("Testing",)
    )
    checkpoint(service, ALICE, task_id="private", title="Python secret", topics=("testing",))
    assert [source.id for source in service.search(PUBLIC, "Python")] == [title_match.id, body_match.id]
    assert [source.id for source in service.search(PUBLIC, "", topics=("TESTING",))] == [title_match.id]
    assert len(service.search(PUBLIC, "Python", limit=1)) == 1


def test_markdown_is_the_only_source_and_navigation_body_store(service):
    source = checkpoint(service, body="Unique source body marker.")
    summary = navigation(service, body=f"Unique navigation marker: [source](memory:{source.id})")
    manifest_path = next(service.root.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == 2
    assert "Unique source body marker" not in manifest_path.read_text()
    assert "Unique navigation marker" not in manifest_path.read_text()
    source_path = manifest_path.parent / "objects" / manifest["sources"][source.id]
    summary_path = manifest_path.parent / "objects" / manifest["navigation"]
    assert source_path.read_text().endswith(source.body + "\n")
    assert summary_path.read_text().endswith(summary.body + "\n")
    assert service.read(PUBLIC, source.id).body == source.body
    assert service.list_navigation(PUBLIC) == [summary]


def test_invalid_and_unsupplied_navigation_references_cannot_publish(service):
    first = checkpoint(service, task_id="a")
    second = checkpoint(service, task_id="b")
    snapshot = service.snapshot_domain(PUBLIC)
    for overrides in (
        {"source_ids": ["0" * 32]},
        {"source_ids": [first.id], "body": "[invented](memory:unknown)"},
        {"source_ids": [first.id], "body": f"[other](memory:{second.id})"},
        {"source_ids": [second.id], "candidate_ids": [first.id]},
        {"input_digest": "different-input"},
    ):
        with pytest.raises(ValueError):
            navigation(service, snapshot=snapshot, **overrides)
        assert service.snapshot_domain(PUBLIC).navigation is None
    valid = navigation(
        service,
        snapshot=snapshot,
        source_ids=[first.id],
        candidate_ids=[first.id],
        body=f"[first](memory:{first.id})",
    )
    assert valid.sources == (first.id,)


@pytest.mark.parametrize("change", ["replace", "add", "remove"])
def test_navigation_rejects_outdated_source_snapshots(service, change):
    original = checkpoint(service)
    snapshot = service.snapshot_domain(PUBLIC)
    if change == "add":
        checkpoint(service, task_id="another-task")
    else:
        checkpoint(
            service,
            input_digest="evidence-v2",
            body="" if change == "remove" else "Updated source evidence.",
            expected_revision=original.revision,
        )
    with pytest.raises(MemoryConflict, match="sources changed"):
        navigation(service, snapshot=snapshot)
    assert service.snapshot_domain(PUBLIC).navigation is None


def test_navigation_cas_rejects_a_second_writer_and_exact_replay_is_a_noop(service, monkeypatch):
    source = checkpoint(service)
    snapshot = service.snapshot_domain(PUBLIC)
    first = navigation(service, snapshot=snapshot)
    with pytest.raises(MemoryConflict, match="current revision"):
        navigation(service, snapshot=snapshot, body=f"A different grouping: [source](memory:{source.id})")

    def unexpected_write(*_):
        pytest.fail("identical navigation was republished")

    monkeypatch.setattr(service, "_atomic_write", unexpected_write)
    assert navigation(service, snapshot=snapshot) == first


@pytest.mark.parametrize("stage", ["source", "navigation"])
def test_failed_manifest_commit_keeps_prior_published_data_and_checkpoint(service, monkeypatch, stage):
    original = checkpoint(service)
    old_navigation = navigation(service)
    original_write = service._atomic_write

    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("simulated disk failure before commit")
        original_write(path, content)

    monkeypatch.setattr(service, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="simulated disk failure"):
        if stage == "source":
            checkpoint(
                service,
                input_digest="evidence-v2",
                complete=False,
                body="Uncommitted partial output.",
                expected_revision=original.revision,
            )
        else:
            navigation(service, body=f"Uncommitted navigation: [source](memory:{original.id})")
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, original.task_id) == original
    assert restarted.read(PUBLIC, original.id).body == original.body
    assert restarted.list_navigation(PUBLIC) == [old_navigation]
    assert restarted.search(PUBLIC, "Uncommitted") == []
    # Orphan object files do not become sources, and the same input can be retried.
    updated = checkpoint(restarted, input_digest="evidence-v2", expected_revision=original.revision)
    assert updated.id == original.id
    assert restarted.source_state(PUBLIC, original.task_id) == updated


def test_navigation_failure_does_not_undo_successful_source_extraction(service, monkeypatch):
    source = checkpoint(service)
    old_navigation = navigation(service)
    updated = checkpoint(
        service, input_digest="evidence-v2", body="Newly extracted fact.", expected_revision=source.revision
    )
    original_write = service._atomic_write

    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("navigation publish failed")
        original_write(path, content)

    monkeypatch.setattr(service, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="navigation publish failed"):
        navigation(service)
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, updated.task_id) == updated
    assert restarted.list_navigation(PUBLIC)[0].revision == old_navigation.revision
    assert restarted.list_navigation(PUBLIC)[0].stale
    assert not navigation(restarted).stale


def test_cleanup_failure_after_commit_does_not_report_failed_publication(service, monkeypatch):
    original = checkpoint(service)
    original_unlink = type(service.root).unlink

    def fail_old_object(path, *args, **kwargs):
        if path.name.endswith(f"-{original.revision}.md"):
            raise OSError("obsolete file is busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(service.root), "unlink", fail_old_object)
    published = checkpoint(
        service, input_digest="evidence-v2", body="Current publication.", expected_revision=original.revision
    )
    assert service.read(PUBLIC, published.id).body == "Current publication."
    assert service.source_state(PUBLIC, published.task_id) == published
    assert len(service.list_sources(PUBLIC)) == 1


def test_concurrent_sources_do_not_lose_each_other_or_share_in_memory_state(service):
    barrier = Barrier(6)

    def produce(index):
        local = MemoryService(service.root)
        barrier.wait(timeout=10)
        return checkpoint(local, task_id=f"task-{index}")

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(produce, range(6)))
    assert {source.id for source in service.list_sources(PUBLIC)} == {source.id for source in results}


def test_concurrent_same_source_writers_only_publish_one_result(service):
    barrier = Barrier(2)

    def produce(body):
        local = MemoryService(service.root)
        barrier.wait(timeout=10)
        try:
            return checkpoint(local, body=body)
        except MemoryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(produce, ["First extraction.", "Second extraction."]))
    winner = next(result for result in results if result)
    assert sum(result is not None for result in results) == 1
    assert service.read(PUBLIC, winner.id).body == winner.body


def test_character_budgets_accept_multibyte_text_and_reject_oversized_output(service):
    source = checkpoint(service, body="源" * SOURCE_BODY_MAX_CHARS)
    assert len(source.body.encode()) > SOURCE_BODY_MAX_CHARS
    with pytest.raises(ValueError, match="16000"):
        checkpoint(service, task_id="too-large", body="源" * (SOURCE_BODY_MAX_CHARS + 1))
    with pytest.raises(ValueError, match="8000"):
        navigation(service, body="导" * (NAVIGATION_MAX_CHARS + 1))
    assert len(service.list_sources(PUBLIC)) == 1
    assert service.list_navigation(PUBLIC) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"cursor": -1},
        {"cursor": True},
        {"complete": 1},
        {"input_digest": ""},
        {"title": "two\nlines"},
        {"body": "embedded\x00nul"},
        {"topics": "not-a-list"},
        {"topics": ["x" * 129]},
        {"topics": [str(index) for index in range(33)]},
        {"sources": "not-a-list"},
    ],
)
def test_invalid_extraction_fields_never_publish(service, overrides):
    with pytest.raises(ValueError):
        checkpoint(service, **overrides)
    assert service.source_state(PUBLIC, "verified-task") is None
    assert service.list_sources(PUBLIC) == []


def test_old_memory_format_is_rejected_instead_of_silently_read_or_rewritten(service):
    directory = service._directory("public")
    directory.mkdir()
    old_manifest = {"domain": "public", "notes": {}, "requests": {}}
    path = directory / "manifest.json"
    path.write_text(json.dumps(old_manifest))
    with pytest.raises(ValueError, match="migrate"):
        service.list_sources(PUBLIC)
    with pytest.raises(ValueError, match="migrate"):
        checkpoint(service)
    assert json.loads(path.read_text()) == old_manifest
