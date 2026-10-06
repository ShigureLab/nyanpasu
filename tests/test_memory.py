from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from pydantic import BaseModel, Field

from nyanpasu.memory import (
    SOURCE_BODY_MAX_CHARS,
    SUMMARY_MAX_BYTES,
    MemoryAccess,
    MemoryConflict,
    MemoryDenied,
    MemoryNotFound,
    MemoryService,
    SummaryTooLarge,
    summary_block,
    validate_summary_block,
)

PUBLIC = MemoryAccess(("public",), "public")
ALICE = MemoryAccess(("public", "private:alice"), "private:alice")
BOB = MemoryAccess(("public", "private:bob"), "private:bob")


def checkpoint(service, access=PUBLIC, task_id="verified-task", **overrides):
    fields = {
        "context_key": "test-context",
        "context_generation": 1,
        "source_order": 0,
        "input_digest": "evidence-v1",
        "cursor": 1,
        "complete": True,
        "title": "Testing workflow",
        "body": "The repository passed its Python tests.",
        "topics": ("Python", "testing"),
        "sources": ("codex:session:turn:result",),
    }
    return service.checkpoint_source(access, task_id, **(fields | overrides))


def summarize(service, access=PUBLIC, snapshot=None, **overrides):
    snapshot = snapshot or service.snapshot_context(access, "test-context", 1)
    fields = {
        "body": "\n".join(f"- [{source.title}](memory:{source.id})" for source in snapshot.sources),
        "source_ids": [source.id for source in snapshot.sources],
        "source_revisions": snapshot.source_revisions,
        "input_digest": snapshot.input_digest,
        "expected_revision": snapshot.summary.revision if snapshot.summary else None,
    }
    return service.publish_summary(access, snapshot.context_key, snapshot.context_generation, **(fields | overrides))


def legacy_sources(service, access=PUBLIC, *, contexts=None):
    """Write the previous source format to exercise an actual on-disk upgrade."""
    directory = service._directory(access.write_domain)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("summaries")
    for collection in (manifest["sources"], manifest["drafts"]):
        for identity, filename in collection.items():
            old_path = directory / "objects" / filename
            header, body = old_path.read_text().removeprefix("---\n").split("\n---\n\n", 1)
            data = json.loads(header)
            for field in ("context_key", "context_generation", "source_order", "task_digests"):
                data.pop(field)
            if contexts is not None:
                key, generation, order = contexts[data["task_id"]]
                data.update(context_key=key, context_generation=generation, source_order=order)
            content = ("---\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n---\n\n" + body).encode()
            revision = hashlib.sha256(content).hexdigest()
            filename = f"{identity}-{revision}.md"
            (directory / "objects" / filename).write_bytes(content)
            collection[identity] = filename
            old_path.unlink()
    old_navigation = directory / "objects" / "legacy-navigation.md"
    old_navigation.write_text("Retired navigation that must never be injected.")
    manifest["navigation"] = old_navigation.name
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path, old_navigation


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


def test_domain_listing_reads_committed_manifests_after_restart(service):
    assert service.list_domains() == ()
    checkpoint(service)
    checkpoint(service, ALICE, complete=False)
    summarize(service, BOB)
    # A failed write may leave a domain directory without a committed manifest.
    service._directory("private:uncommitted").mkdir()
    restarted = MemoryService(service.root)
    assert restarted.list_domains() == ("private:alice", "private:bob", "public")
    assert {source.domain for source in restarted.list_sources(PUBLIC)} == {"public"}


def test_sources_and_summary_are_invisible_outside_the_authorized_audience(service):
    shared = checkpoint(service)
    alice = checkpoint(service, ALICE, body="Alice uses a private staging runner.")
    bob = checkpoint(service, BOB, body="Bob uses his private runner.")
    summarize(service)
    summarize(service, ALICE)
    summarize(service, BOB)

    assert {source.id for source in service.search(ALICE, "runner")} == {alice.id}
    assert {source.id for source in service.list_sources(PUBLIC)} == {shared.id}
    assert service.search(PUBLIC, "Alice") == []
    assert service.search(ALICE, "Bob") == []
    assert service.search(MemoryAccess(), "runner") == []
    assert {item.domain for item in service.list_summaries(ALICE)} == {"public", "private:alice"}
    assert service.list_summaries(MemoryAccess()) == []
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
    assert service.source_state(ALICE, shared.context_key) is None
    assert service.snapshot_context(ALICE, "test-context", 1).sources == ()
    private = checkpoint(service, ALICE)
    assert private.id != shared.id
    assert [source.id for source in service.snapshot_context(ALICE, "test-context", 1).sources] == [private.id]
    with pytest.raises(ValueError, match="supplied source"):
        summarize(service, ALICE, source_ids=[shared.id])
    assert service.snapshot_context(ALICE, "test-context", 1).summary is None


def test_read_access_never_grants_background_writes_or_checkpoint_reads(service):
    checkpoint(service)
    reader = MemoryAccess(("public",))
    assert len(service.list_sources(reader)) == 1
    for operation in (
        lambda: checkpoint(service, reader),
        lambda: service.source_state(reader, "verified-task"),
        lambda: service.snapshot_context(reader, "test-context", 1),
        lambda: service.publish_summary(
            reader, "test-context", 1, body="", source_ids=[], source_revisions={}, input_digest="x"
        ),
    ):
        with pytest.raises(MemoryDenied):
            operation()


def test_partial_extraction_is_durable_but_not_recalled_until_complete(service):
    partial = checkpoint(service, cursor=1, complete=False, body="First chunk result.")
    restarted = MemoryService(service.root)
    restored = restarted.source_state(PUBLIC, partial.context_key)
    assert restored == partial
    assert restored is not None and restored.cursor == 1 and not restored.complete
    assert restarted.list_sources(PUBLIC) == []
    assert restarted.snapshot_context(PUBLIC, "test-context", 1).sources == ()
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
    earlier = checkpoint(service, task_id="earlier-task")
    original = checkpoint(service, expected_revision=earlier.revision)
    old_summary = summarize(service)
    pending = checkpoint(
        service,
        input_digest="evidence-v2",
        complete=False,
        body="A newer partial account.",
        expected_revision=original.revision,
    )
    assert pending.id == original.id
    assert pending.task_digests == {earlier.task_id: earlier.input_digest}
    assert service.source_state(PUBLIC, original.context_key).task_digests == pending.task_digests
    assert service.published_source_state(PUBLIC, original.context_key).task_digests == original.task_digests
    assert service.read(PUBLIC, original.id).revision == original.revision
    assert service.list_summaries(PUBLIC) == [old_summary]
    updated = checkpoint(
        service,
        input_digest="evidence-v2",
        cursor=2,
        body="A complete newer account.",
        expected_revision=pending.revision,
    )
    assert updated.id == original.id
    assert updated.task_digests == {earlier.task_id: earlier.input_digest, original.task_id: "evidence-v2"}
    assert service.read(PUBLIC, original.id).body == updated.body
    assert service.list_summaries(PUBLIC)[0].stale
    current = summarize(service)
    assert not current.stale
    assert current.input_digest == service.snapshot_context(PUBLIC, "test-context", 1).input_digest


def test_pending_followup_preserves_history_and_cannot_be_overwritten_by_another_task(service):
    original = checkpoint(service)
    pending = checkpoint(
        service,
        task_id="followup",
        complete=False,
        body="Prior and partial follow-up evidence.",
        sources=["tool:followup:first"],
        expected_revision=original.revision,
    )
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, "test-context") == pending
    assert restarted.published_source_state(PUBLIC, "test-context") == original
    assert pending.task_digests == original.task_digests
    assert set(original.sources) <= set(pending.sources)
    with pytest.raises(MemoryConflict, match="pending source task"):
        checkpoint(restarted, task_id="newer", expected_revision=pending.revision)
    assert restarted.source_state(PUBLIC, "test-context") == pending

    completed = checkpoint(
        restarted,
        task_id="followup",
        cursor=2,
        body="Prior and complete follow-up evidence.",
        sources=["tool:followup:final"],
        expected_revision=pending.revision,
    )
    assert completed.id == original.id
    assert set(pending.sources) | {"tool:followup:final"} == set(completed.sources)
    assert completed.task_digests == {original.task_id: original.input_digest, "followup": "evidence-v1"}
    assert restarted.published_source_state(PUBLIC, "test-context") == completed
    assert len(restarted.list_sources(PUBLIC)) == 1


def test_unpublished_account_is_not_returned_as_a_publication(service):
    checkpoint(service, complete=False)
    assert service.published_source_state(PUBLIC, "test-context") is None
    with pytest.raises(MemoryDenied):
        service.published_source_state(MemoryAccess(("public",)), "test-context")


def test_empty_completed_account_is_a_persistent_receipt_and_removes_old_recall(service):
    original = checkpoint(service)
    summarize(service)
    empty = checkpoint(service, input_digest="evidence-v2", body="", expected_revision=original.revision)
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, original.context_key) == empty
    assert empty.complete and empty.body == ""
    assert restarted.list_sources(PUBLIC) == []
    assert restarted.describe(PUBLIC)["count"] == 0
    assert restarted.list_summaries(PUBLIC)[0].stale
    assert checkpoint(restarted, input_digest="evidence-v2", body="") == empty
    with pytest.raises(MemoryNotFound):
        restarted.read(PUBLIC, original.id)
    cleared = summarize(restarted, body="No retained sources.")
    assert cleared.sources == ()
    assert not restarted.list_summaries(PUBLIC)[0].stale


def test_new_tasks_update_one_context_account_and_keep_prior_provenance(service):
    first = checkpoint(service, task_id="task-a")
    second = checkpoint(service, task_id="task-b", source_order=20, expected_revision=first.revision)
    assert first.id == second.id and first.revision != second.revision
    assert service.search(PUBLIC, "tests") == [service.read(PUBLIC, first.id)]
    assert second.task_id == "task-b" and second.source_order == 20
    assert {"task:task-a", "task:task-b"} <= set(second.sources)
    assert second.task_digests == {"task-a": "evidence-v1", "task-b": "evidence-v1"}
    with pytest.raises(MemoryConflict, match="already complete"):
        checkpoint(service, task_id="task-a", expected_revision=second.revision)


def test_backfilled_tasks_preserve_the_latest_evidence_order(service):
    newer = checkpoint(service, task_id="newer", source_order=30)
    pending = checkpoint(service, task_id="backfill", source_order=10, complete=False, expected_revision=newer.revision)
    assert (pending.task_id, pending.source_order) == ("backfill", 30)
    backfilled = checkpoint(service, task_id="backfill", source_order=10, cursor=2, expected_revision=pending.revision)
    assert (backfilled.task_id, backfilled.source_order) == ("backfill", 30)
    intermediate = checkpoint(service, task_id="intermediate", source_order=20, expected_revision=backfilled.revision)
    assert (intermediate.task_id, intermediate.source_order) == ("intermediate", 30)
    latest = checkpoint(service, task_id="latest", source_order=40, expected_revision=intermediate.revision)
    assert latest.source_order == 40
    assert len(service.list_sources(PUBLIC)) == 1


def test_rolling_account_can_be_found_by_earlier_task_ids_and_context(service):
    first = checkpoint(service, task_id="previous_task_123", context_key="scope_unique_456")
    latest = checkpoint(
        service,
        task_id="current_task_789",
        context_key=first.context_key,
        title="Retained lessons",
        body="Earlier and newer evidence.",
        expected_revision=first.revision,
    )
    recalled = service.read(PUBLIC, latest.id)
    assert latest.task_id != first.task_id
    assert service.search(PUBLIC, first.task_id) == [recalled]
    assert service.search(PUBLIC, latest.task_id) == [recalled]
    assert service.search(PUBLIC, first.context_key) == [recalled]


def test_source_identity_is_stable_and_does_not_become_a_filesystem_path(service):
    source = checkpoint(service, task_id="../../elsewhere/源任务")
    assert source.task_id == "../../elsewhere/源任务"
    assert len(source.id) == 32
    assert MemoryService(service.root).source_state(PUBLIC, source.context_key) == source
    assert not (service.root.parent / "elsewhere").exists()


def test_topic_filter_and_weighted_retrieval_are_scoped_and_search_source_content(service):
    body_match = checkpoint(
        service,
        task_id="a",
        context_key="context-a",
        title="General result",
        body="A Python result",
        topics=("runtime",),
    )
    title_match = checkpoint(
        service,
        task_id="b",
        context_key="context-b",
        title="Python workflow",
        body="Verified workflow",
        topics=("Testing",),
    )
    checkpoint(service, ALICE, task_id="private", title="Python secret", topics=("testing",))
    assert [source.id for source in service.search(PUBLIC, "Python")] == [title_match.id, body_match.id]
    assert [source.id for source in service.search(PUBLIC, "", topics=("TESTING",))] == [title_match.id]
    assert len(service.search(PUBLIC, "Python", limit=1)) == 1


@pytest.mark.parametrize("excluded_by", ["domain", "topics"])
def test_rare_domain_terms_rank_first_using_only_the_searchable_corpus(service, excluded_by):
    rare = checkpoint(
        service,
        task_id="receipt",
        context_key="receipt-account",
        title="Z receipt",
        body="lkey lifetime policy",
        topics=("runtime",),
    )
    common = [
        checkpoint(
            service,
            task_id=f"common-{index}",
            context_key=f"common-{index}",
            title=f"A result {index}",
            body="worker runtime policy",
            topics=("runtime",),
        )
        for index in range(6)
    ]
    query = "worker runtime lkey"
    baseline = [source.id for source in service.search(PUBLIC, query, topics=("runtime",))]
    assert baseline[0] == rare.id
    # Different matching terms still recall both kinds of prior evidence.
    assert set(baseline) == {rare.id, *(source.id for source in common)}

    for index in range(12):
        checkpoint(
            service,
            ALICE if excluded_by == "domain" else PUBLIC,
            task_id=f"excluded-{index}",
            context_key=f"excluded-{index}",
            title="Other receipt",
            body="lkey lifetime policy",
            topics=("runtime",) if excluded_by == "domain" else ("unrelated",),
        )

    assert [source.id for source in service.search(PUBLIC, query, topics=("runtime",))] == baseline


def test_provenance_does_not_drive_relevance_but_exact_historical_task_lookup_still_works(service):
    previous = checkpoint(
        service,
        task_id="previous-task-285",
        context_key="receipt-account",
        title="Receipt contract",
        body="Verify adapter digest lifetime.",
    )
    latest = checkpoint(
        service,
        task_id="latest-task-285",
        context_key=previous.context_key,
        title=previous.title,
        body=previous.body,
        expected_revision=previous.revision,
    )
    unrelated = checkpoint(
        service,
        task_id="dashboard-task",
        context_key="dashboard-account",
        title="A dashboard result",
        body="Typography was checked in previous-task-285.",
    )
    baseline = [source.id for source in service.search(PUBLIC, "adapter")]
    assert baseline == [latest.id]
    checkpoint(
        service,
        task_id="dashboard-task-next",
        context_key=unrelated.context_key,
        title=unrelated.title,
        body=unrelated.body,
        sources=tuple(f"task:adapter-investigation-{index}" for index in range(100)),
        expected_revision=unrelated.revision,
    )

    assert [source.id for source in service.search(PUBLIC, "adapter")] == baseline
    assert [source.id for source in service.search(PUBLIC, previous.task_id)] == [latest.id]


def test_markdown_is_the_only_source_and_summary_body_store(service):
    source = checkpoint(service, body="Unique source body marker.")
    summary = summarize(service, body=f"Unique summary marker: [source](memory:{source.id})")
    manifest_path = next(service.root.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert "Unique source body marker" not in manifest_path.read_text()
    assert "Unique summary marker" not in manifest_path.read_text()
    source_path = manifest_path.parent / "objects" / manifest["sources"][source.id]
    summary_path = manifest_path.parent / "objects" / manifest["summaries"][summary.id]
    assert source_path.read_text().endswith(source.body + "\n")
    assert summary_path.read_text().endswith(summary.body + "\n")
    assert service.read(PUBLIC, source.id).body == source.body
    assert service.list_summaries(PUBLIC) == [summary]


def test_invalid_and_unsupplied_summary_references_cannot_publish(service):
    first = checkpoint(service, task_id="a")
    second = checkpoint(service, task_id="b", context_key="foreign-context")
    snapshot = service.snapshot_context(PUBLIC, "test-context", 1)
    for overrides in (
        {"source_ids": ["0" * 32]},
        {"source_ids": [first.id], "body": "[invented](memory:unknown)"},
        {"source_ids": [first.id], "body": f"[other](memory:{second.id})"},
        {"source_ids": [second.id], "candidate_ids": [first.id]},
        {"input_digest": "different-input"},
    ):
        with pytest.raises(ValueError):
            summarize(service, snapshot=snapshot, **overrides)
        assert service.snapshot_context(PUBLIC, "test-context", 1).summary is None
    valid = summarize(
        service,
        snapshot=snapshot,
        source_ids=[first.id],
        candidate_ids=[first.id],
        body=f"[first](memory:{first.id})",
    )
    assert valid.sources == (first.id,)


@pytest.mark.parametrize("change", ["replace", "add", "remove"])
def test_summary_rejects_outdated_source_snapshots(service, change):
    original = checkpoint(service)
    snapshot = service.snapshot_context(PUBLIC, "test-context", 1)
    if change == "add":
        checkpoint(
            service, task_id="another-task", body="Additional verified evidence.", expected_revision=original.revision
        )
    else:
        checkpoint(
            service,
            input_digest="evidence-v2",
            body="" if change == "remove" else "Updated source evidence.",
            expected_revision=original.revision,
        )
    with pytest.raises(MemoryConflict, match="sources changed"):
        summarize(service, snapshot=snapshot)
    assert service.snapshot_context(PUBLIC, "test-context", 1).summary is None


def test_summary_cas_rejects_a_second_writer_and_exact_replay_is_a_noop(service, monkeypatch):
    source = checkpoint(service)
    snapshot = service.snapshot_context(PUBLIC, "test-context", 1)
    first = summarize(service, snapshot=snapshot)
    with pytest.raises(MemoryConflict, match="current revision"):
        summarize(service, snapshot=snapshot, body=f"A different grouping: [source](memory:{source.id})")

    def unexpected_write(*_):
        pytest.fail("identical summary was republished")

    monkeypatch.setattr(service, "_atomic_write", unexpected_write)
    assert summarize(service, snapshot=snapshot) == first


@pytest.mark.parametrize("stage", ["source", "summary"])
def test_failed_manifest_commit_keeps_prior_published_data_and_checkpoint(service, monkeypatch, stage):
    original = checkpoint(service)
    old_summary = summarize(service)
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
            summarize(service, body=f"Uncommitted summary: [source](memory:{original.id})")
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, original.context_key) == original
    assert restarted.read(PUBLIC, original.id).body == original.body
    assert restarted.list_summaries(PUBLIC) == [old_summary]
    assert restarted.search(PUBLIC, "Uncommitted") == []
    # Orphan object files do not become sources, and the same input can be retried.
    updated = checkpoint(restarted, input_digest="evidence-v2", expected_revision=original.revision)
    assert updated.id == original.id
    assert restarted.source_state(PUBLIC, original.context_key) == updated


def test_summary_failure_does_not_undo_successful_source_extraction(service, monkeypatch):
    source = checkpoint(service)
    old_summary = summarize(service)
    updated = checkpoint(
        service, input_digest="evidence-v2", body="Newly extracted fact.", expected_revision=source.revision
    )
    original_write = service._atomic_write

    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("summary publish failed")
        original_write(path, content)

    monkeypatch.setattr(service, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="summary publish failed"):
        summarize(service)
    restarted = MemoryService(service.root)
    assert restarted.source_state(PUBLIC, updated.context_key) == updated
    assert restarted.list_summaries(PUBLIC)[0].revision == old_summary.revision
    assert restarted.list_summaries(PUBLIC)[0].stale
    assert not summarize(restarted).stale


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
    assert service.source_state(PUBLIC, published.context_key) == published
    assert len(service.list_sources(PUBLIC)) == 1


def test_concurrent_sources_do_not_lose_each_other_or_share_in_memory_state(service):
    barrier = Barrier(6)

    def produce(index):
        local = MemoryService(service.root)
        barrier.wait(timeout=10)
        return checkpoint(local, task_id=f"task-{index}", context_key=f"context-{index}")

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


def test_source_character_budget_and_summary_byte_budget_reject_oversized_output(service):
    source = checkpoint(service, body="源" * SOURCE_BODY_MAX_CHARS)
    assert len(source.body.encode()) > SOURCE_BODY_MAX_CHARS
    with pytest.raises(ValueError, match="16000"):
        checkpoint(service, task_id="too-large", body="源" * (SOURCE_BODY_MAX_CHARS + 1))
    with pytest.raises(ValueError, match="1024"):
        summarize(service, body="导" * (SUMMARY_MAX_BYTES + 1))
    assert len(service.list_sources(PUBLIC)) == 1
    assert service.list_summaries(PUBLIC) == []


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
    assert service.source_state(PUBLIC, "test-context") is None
    assert service.list_sources(PUBLIC) == []


def test_context_snapshots_roll_forward_and_keep_generations_separate(service):
    earlier = checkpoint(service, task_id="z-earlier", source_order=10, title="Alphabetically last")
    later = checkpoint(
        service, task_id="a-later", source_order=20, title="Alphabetically first", expected_revision=earlier.revision
    )
    next_generation = checkpoint(service, task_id="next", context_generation=2)
    other_context = checkpoint(service, task_id="other", context_key="another-context")
    private = checkpoint(service, ALICE, task_id="private")

    snapshot = service.snapshot_context(PUBLIC, "test-context", 1)
    assert snapshot.sources == (service.read(PUBLIC, later.id),)
    assert snapshot.source_revisions == {later.id: later.content_revision}
    assert len({earlier.id, later.id, next_generation.id, other_context.id, private.id}) == 4
    assert service.snapshot_context(PUBLIC, "test-context", 2).sources == (service.read(PUBLIC, next_generation.id),)
    assert service.snapshot_context(PUBLIC, "another-context", 1).sources == (service.read(PUBLIC, other_context.id),)
    assert service.snapshot_context(ALICE, "test-context", 1).sources == (service.read(ALICE, private.id),)
    assert service.source_state(PUBLIC, "test-context", 2) == next_generation
    assert service.published_source_state(PUBLIC, "test-context") == later


def test_summary_identity_and_staleness_are_per_domain_context_and_generation(service):
    first = checkpoint(service)
    summaries = [summarize(service)]
    for access, context, generation in (
        (PUBLIC, "test-context", 2),
        (PUBLIC, "another-context", 1),
        (ALICE, "test-context", 1),
    ):
        checkpoint(
            service, access, task_id=f"{context}:{generation}", context_key=context, context_generation=generation
        )
        summaries.append(summarize(service, access, service.snapshot_context(access, context, generation)))
    assert len({summary.id for summary in summaries}) == 4
    assert not any(summary.stale for summary in service.list_summaries(ALICE))

    checkpoint(service, input_digest="corrected", body="Corrected evidence.", expected_revision=first.revision)
    states = {summary.id: summary.stale for summary in service.list_summaries(ALICE)}
    assert states == {summary.id: summary.id == summaries[0].id for summary in summaries}
    assert len(service.list_summaries(PUBLIC)) == 3


@pytest.mark.parametrize("foreign_scope", ["context", "generation", "domain"])
def test_summary_rejects_sources_from_another_context_generation_or_domain(service, foreign_scope):
    own = checkpoint(service)
    foreign = checkpoint(
        service,
        ALICE if foreign_scope == "domain" else PUBLIC,
        task_id="foreign",
        context_key="other-context" if foreign_scope == "context" else own.context_key,
        context_generation=2 if foreign_scope == "generation" else own.context_generation,
    )
    snapshot = service.snapshot_context(PUBLIC, own.context_key, own.context_generation)
    with pytest.raises(ValueError, match="supplied source"):
        summarize(service, snapshot=snapshot, source_ids=[foreign.id])
    forged = replace(snapshot, sources=(*snapshot.sources, foreign))
    with pytest.raises(MemoryConflict, match="sources changed"):
        summarize(service, snapshot=forged)
    assert service.list_summaries(PUBLIC) == []


def test_full_summary_block_budget_includes_wrapper_and_omits_internal_metadata(service):
    long_context = "internal-context/" * 1000
    checkpoint(service, context_key=long_context)
    snapshot = service.snapshot_context(PUBLIC, long_context, 1)
    first = summarize(service, snapshot=snapshot, body="")
    overhead = len(summary_block(first).encode("utf-8"))
    body = "a" * (SUMMARY_MAX_BYTES - overhead)
    exact = summarize(service, snapshot=service.snapshot_context(PUBLIC, long_context, 1), body=body)
    assert len(summary_block(exact).encode("utf-8")) == SUMMARY_MAX_BYTES
    assert json.loads(summary_block(exact)) == {"id": exact.id, "body": exact.body}
    assert long_context not in summary_block(exact)
    validate_summary_block(exact.id, exact.body)

    with pytest.raises(SummaryTooLarge, match="1025 bytes"):
        summarize(service, snapshot=service.snapshot_context(PUBLIC, long_context, 1), body=body + "a")
    with pytest.raises(SummaryTooLarge):
        validate_summary_block(exact.id, body + "a")
    assert service.list_summaries(PUBLIC) == [exact]


@pytest.mark.parametrize("unit", ["汉", "🙂", '"', "\\", "\nline"])
def test_summary_budget_counts_utf8_and_json_escaping_before_publication(service, unit):
    checkpoint(service)
    empty = summarize(service, body="")
    body = "Scope: "
    while True:
        candidate = body + unit
        try:
            validate_summary_block(empty.id, candidate)
        except SummaryTooLarge:
            break
        body = candidate
    published = summarize(service, body=body)
    assert len(summary_block(published).encode("utf-8")) <= SUMMARY_MAX_BYTES
    manifest_before = next(service.root.glob("*/manifest.json")).read_bytes()
    with pytest.raises(SummaryTooLarge):
        summarize(service, body=candidate)
    assert next(service.root.glob("*/manifest.json")).read_bytes() == manifest_before
    assert service.list_summaries(PUBLIC) == [published]


def test_legacy_sources_read_without_writes_and_first_write_retires_navigation(service):
    source = checkpoint(service)
    manifest_path, old_navigation = legacy_sources(service)
    before = manifest_path.read_bytes()
    restarted = MemoryService(service.root)
    recalled = restarted.read(PUBLIC, source.id)
    assert (recalled.context_key, recalled.context_generation, recalled.source_order) == (source.task_id, 1, 0)
    assert recalled.body == source.body and recalled.sources == source.sources
    assert restarted.list_summaries(PUBLIC) == []
    assert manifest_path.read_bytes() == before
    assert old_navigation.exists()

    checkpoint(restarted, task_id="new-task", context_key="new-context")
    manifest = json.loads(manifest_path.read_text())
    assert "navigation" not in manifest
    assert manifest["summaries"] == {}
    assert not old_navigation.exists()
    assert restarted.read(PUBLIC, source.id) == recalled


def test_migration_preserves_published_history_but_restarts_legacy_drafts(service, monkeypatch):
    source = checkpoint(service)
    checkpoint(
        service,
        input_digest="new-evidence",
        complete=False,
        body="Draft update.",
        expected_revision=source.revision,
    )
    legacy_sources(service)
    old_published = service.read(PUBLIC, source.id)
    mapping = {source.task_id: ("review:pr-257", 3, 1234.5)}
    assert service.bind_source_contexts(PUBLIC, mapping) == 1
    published = service.read(PUBLIC, source.id)
    current = service.source_state(PUBLIC, "review:pr-257", 3)
    assert current == service.published_source_state(PUBLIC, "review:pr-257", 3)
    assert current.complete and current.input_digest == "evidence-v1"
    assert current.task_digests == {}
    assert (published.context_key, published.context_generation, published.source_order) == mapping[source.task_id]
    assert published.id != old_published.id
    assert published.body == old_published.body and published.sources == old_published.sources
    assert published.updated_at == old_published.updated_at
    assert service.snapshot_context(PUBLIC, "review:pr-257", 3).sources == (published,)

    def unexpected_write(*_):
        pytest.fail("already bound source metadata was rewritten")

    monkeypatch.setattr(service, "_atomic_write", unexpected_write)
    assert service.bind_source_contexts(PUBLIC, mapping) == 0
    assert service.bind_source_contexts(PUBLIC, {source.task_id: ("wrong-context", 8, 5000)}) == 0
    assert service.read(PUBLIC, source.id) == published
    assert service.source_state(PUBLIC, "review:pr-257", 3) == current

    updated = checkpoint(
        MemoryService(service.root),
        context_key="review:pr-257",
        context_generation=3,
        source_order=1234.5,
        input_digest="new-evidence",
        expected_revision=current.revision,
        body="Retained history with the completed update.",
    )
    assert updated.task_digests == {source.task_id: "new-evidence"}
    assert service.read(PUBLIC, source.id).body == updated.body


def test_migration_drops_a_legacy_only_draft_without_inventing_a_publication(service):
    draft = checkpoint(service, complete=False)
    legacy_sources(service)
    mapping = {draft.task_id: (draft.context_key, draft.context_generation, draft.source_order)}
    assert service.bind_source_contexts(PUBLIC, mapping) == 1
    assert service.source_state(PUBLIC, draft.context_key) is None
    assert service.published_source_state(PUBLIC, draft.context_key) is None
    assert service.list_sources(PUBLIC) == []
    assert service.bind_source_contexts(PUBLIC, mapping) == 0


def test_migration_preserves_a_current_rolling_draft_and_its_receipts(service):
    original = checkpoint(service)
    draft = checkpoint(service, task_id="followup", complete=False, expected_revision=original.revision)
    mapping = {draft.task_id: (draft.context_key, draft.context_generation, draft.source_order)}
    assert service.bind_source_contexts(PUBLIC, mapping) == 0
    assert service.source_state(PUBLIC, draft.context_key) == draft
    assert service.published_source_state(PUBLIC, draft.context_key) == original


def test_migration_merges_all_task_text_and_receipts_and_keeps_old_links_readable(service):
    later = checkpoint(
        service, task_id="later", context_key="legacy-later", title="Later lesson", body="later evidence " * 1000
    )
    earlier = checkpoint(
        service, task_id="earlier", context_key="legacy-earlier", title="Earlier lesson", body="early evidence " * 1000
    )
    empty = checkpoint(service, task_id="empty", context_key="legacy-empty", body="")
    contexts = {"earlier": ("shared", 2, 10), "later": ("shared", 2, 20), "empty": ("shared", 2, 30)}
    legacy_sources(service, contexts=contexts)
    snapshot = service.snapshot_context(PUBLIC, "shared", 2)
    old_summary = summarize(service, snapshot=snapshot)
    assert not old_summary.stale

    assert service.bind_source_contexts(PUBLIC, contexts) == 3
    current = service.source_state(PUBLIC, "shared", 2)
    assert current is not None and current.complete
    assert current.task_id == "empty" and current.source_order == 30
    assert len(current.body) > SOURCE_BODY_MAX_CHARS
    assert earlier.body in current.body and later.body in current.body
    assert current.body.index(earlier.body) < current.body.index(later.body)
    assert current.task_digests == {item.task_id: item.input_digest for item in (earlier, later, empty)}
    assert set(current.sources) == set(earlier.sources) | set(later.sources) | set(empty.sources)
    assert {source.id for source in service.list_sources(PUBLIC)} == {current.id}
    for old in (earlier, later, empty):
        assert service.read(PUBLIC, old.id).id == current.id
        with pytest.raises(MemoryNotFound):
            service.read(MemoryAccess(("private:alice",)), old.id)
    migrated_summary = service.snapshot_context(PUBLIC, "shared", 2).summary
    assert migrated_summary == replace(old_summary, stale=True)
    assert service.bind_source_contexts(PUBLIC, contexts) == 0

    updated = checkpoint(
        service,
        task_id="followup",
        context_key="shared",
        context_generation=2,
        body="Consolidated earlier lessons with a new verified lesson.",
        expected_revision=current.revision,
    )
    assert service.read(PUBLIC, earlier.id).body == updated.body
    assert service.read(PUBLIC, later.id).body == updated.body
    assert updated.task_digests == {**current.task_digests, "followup": "evidence-v1"}


def test_migration_does_not_mark_an_already_stale_summary_fresh(service):
    original = checkpoint(service, context_key="legacy")
    summary = summarize(service, snapshot=service.snapshot_context(PUBLIC, "legacy", 1))
    checkpoint(
        service,
        context_key="legacy",
        input_digest="corrected",
        body="Corrected lesson.",
        expected_revision=original.revision,
    )
    directory = service._directory(PUBLIC.write_domain)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    filename = manifest["sources"][original.id]
    header, body = (directory / "objects" / filename).read_text().removeprefix("---\n").split("\n---\n\n", 1)
    data = json.loads(header)
    data.pop("task_digests")
    (directory / "objects" / filename).write_text("---\n" + json.dumps(data) + "\n---\n\n" + body)
    assert service.bind_source_contexts(PUBLIC, {}) == 1
    assert service.list_summaries(PUBLIC) == [replace(summary, stale=True)]


def test_current_legacy_summary_migrates_without_rewriting_its_content(service):
    original = checkpoint(service)
    summary = summarize(service)
    directory = service._directory(PUBLIC.write_domain)
    manifest = json.loads((directory / "manifest.json").read_text())
    path = directory / "objects" / manifest["summaries"][summary.id]
    header, body = path.read_text().removeprefix("---\n").split("\n---\n\n", 1)
    data = json.loads(header)
    data["source_revisions"] = {original.id: original.revision}
    legacy = "---\n" + json.dumps(data) + "\n---\n\n" + body
    legacy_path = path.with_name(f"{summary.id}-{hashlib.sha256(legacy.encode()).hexdigest()}.md")
    path.unlink()
    legacy_path.write_text(legacy)
    manifest["summaries"][summary.id] = legacy_path.name
    (directory / "manifest.json").write_text(json.dumps(manifest))

    restarted = MemoryService(service.root)
    assert restarted.bind_source_contexts(PUBLIC, {}) == 0
    migrated = restarted.list_summaries(PUBLIC)[0]
    assert migrated.body == summary.body and migrated.sources == summary.sources
    assert migrated.source_revisions == {original.id: original.content_revision}
    assert migrated.updated_at == summary.updated_at and not migrated.stale
    assert restarted.source_state(PUBLIC, "test-context") == original

    checkpoint(restarted, task_id="noop-followup", expected_revision=original.revision)
    assert restarted.list_summaries(PUBLIC) == [migrated]


def test_failed_legacy_binding_preserves_old_manifest_and_is_retryable(service, monkeypatch):
    source = checkpoint(service)
    manifest_path, old_navigation = legacy_sources(service)
    original = service.read(PUBLIC, source.id)
    before = manifest_path.read_bytes()
    write = service._atomic_write

    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("binding commit failed")
        write(path, content)

    monkeypatch.setattr(service, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="binding commit failed"):
        service.bind_source_contexts(PUBLIC, {source.task_id: ("bound-context", 2, 10)})
    assert manifest_path.read_bytes() == before
    assert old_navigation.exists()
    restarted = MemoryService(service.root)
    assert restarted.read(PUBLIC, source.id) == original
    assert restarted.bind_source_contexts(PUBLIC, {source.task_id: ("bound-context", 2, 10)}) == 1
    assert restarted.read(PUBLIC, source.id).context_key == "bound-context"


def test_legacy_binding_only_changes_the_contribution_domain(service):
    shared = checkpoint(service)
    private = checkpoint(service, ALICE)
    legacy_sources(service)
    legacy_sources(service, ALICE)
    public_before = service.read(PUBLIC, shared.id)
    mapping = {shared.task_id: ("bound-private-context", 2, 15)}
    assert service.bind_source_contexts(ALICE, mapping) == 1
    assert service.read(PUBLIC, shared.id) == public_before
    assert service.read(ALICE, private.id).context_key == "bound-private-context"
    with pytest.raises(MemoryDenied):
        service.bind_source_contexts(MemoryAccess(("public",)), mapping)


@pytest.mark.parametrize(
    "overrides",
    [
        {"context_key": ""},
        {"context_key": "embedded\x00nul"},
        {"context_generation": 0},
        {"context_generation": True},
        {"source_order": -1},
        {"source_order": float("nan")},
        {"source_order": float("inf")},
        {"source_order": True},
    ],
)
def test_invalid_source_context_metadata_never_publishes(service, overrides):
    with pytest.raises(ValueError):
        checkpoint(service, **overrides)
    assert service.list_sources(PUBLIC) == []
