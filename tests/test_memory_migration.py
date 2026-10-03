from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from nyanpasu.memory import MemoryAccess, MemoryService
from nyanpasu.memory_migration import migrate_memory

if TYPE_CHECKING:
    from pathlib import Path


def legacy_note(root: Path, domain: str, note_id: str, body: str, *, title: str = "Earlier memory") -> Path:
    directory = root / hashlib.sha256(domain.encode()).hexdigest()
    (directory / "objects").mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": note_id,
        "key": "legacy-fact",
        "title": title,
        "topics": ["runtime"],
        "applies_to": ["repository:Example", "version:1"],
        "sources": ["task:original"],
        "updated_at": "2026-10-01T00:00:00+00:00",
        "merged_from": [],
    }
    raw = ("---\n" + json.dumps(metadata) + "\n---\n\n" + body + "\n").encode()
    filename = f"{note_id}-{hashlib.sha256(raw).hexdigest()}.md"
    (directory / "objects" / filename).write_bytes(raw)
    manifest = directory / "manifest.json"
    value: dict[str, Any] = (
        json.loads(manifest.read_text()) if manifest.exists() else {"domain": domain, "notes": {}, "requests": {}}
    )
    value["notes"][note_id] = filename
    manifest.write_text(json.dumps(value))
    return manifest


def test_import_preserves_audiences_provenance_and_large_content(tmp_path):
    original = tmp_path / "old"
    destination = tmp_path / "new"
    legacy_note(original, "public", "1" * 32, "public lesson")
    private_body = "Only Alice can read this detailed source.\n" * 1000
    legacy_note(original, "private:alice", "2" * 32, private_body)
    before = {str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()}

    counts = migrate_memory(original, destination)

    assert counts["domains"] == counts["notes"] == 2
    assert counts["sources"] > 2
    assert before == {
        str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()
    }
    service = MemoryService(destination)
    public = service.list_sources(MemoryAccess(("public",)))
    assert len(public) == 1 and "public lesson" in public[0].body
    assert not service.search(MemoryAccess(("public",)), "Alice")
    private = sorted(
        service.list_sources(MemoryAccess(("private:alice",))), key=lambda source: int(source.task_id.rsplit(":", 1)[1])
    )
    restored = "".join(
        source.body.split("\n\n", 1)[1].removesuffix("<!-- End of imported part -->") for source in private
    )
    assert private_body in restored
    assert "repository:Example" in restored and "version:1" in restored
    assert "not independently reverified" in restored
    assert all("task:original" in source.sources and source.complete for source in private)
    assert service.list_navigation(MemoryAccess(("public", "private:alice"))) == []


def test_import_normalizes_display_title_without_changing_legacy_evidence(tmp_path):
    original, destination = tmp_path / "old", tmp_path / "new"
    body = "Original legacy body.\nSecond line."
    legacy_note(original, "public", "1" * 32, body, title="Alpha\rBeta")
    before = {str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()}

    migrate_memory(original, destination)

    [source] = MemoryService(destination).list_sources(MemoryAccess(("public",)))
    assert source.title == "Alpha Beta"
    metadata = json.loads(source.body.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert metadata["title"] == "Alpha\rBeta"
    assert f"Original content:\n{body}\n" in source.body
    assert before == {
        str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()
    }


def test_invalid_import_never_publishes_partial_destination(tmp_path):
    original, destination = tmp_path / "old", tmp_path / "new"
    manifest = legacy_note(original, "public", "1" * 32, "knowledge")
    data = json.loads(manifest.read_text())
    data["notes"]["2" * 32] = "../../outside.md"
    manifest.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="must not contain a path"):
        migrate_memory(original, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".memory-import-*"))
    assert manifest.is_file()


def test_import_never_overwrites_an_existing_store(tmp_path):
    original, destination = tmp_path / "old", tmp_path / "new"
    original.mkdir()
    destination.mkdir()
    marker = destination / "keep"
    marker.write_text("existing")
    with pytest.raises(ValueError, match="must be new"):
        migrate_memory(original, destination)
    assert marker.read_text() == "existing"
