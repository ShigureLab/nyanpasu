from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from nyanpasu.memory import MemoryAccess, MemoryService


def migrate_memory(source: Path, destination: Path) -> dict[str, int]:
    """Import an offline note store into a new source store without changing it.

    The destination is published only after every active note has been imported.
    Imported content retains its original audience and is explicitly unverified.
    This migration is an operator command, never a runtime read fallback.
    """
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ValueError("source must be an existing memory directory")
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("destination must be new and separate from the source directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".memory-import-", dir=destination.parent))
    counts = {"domains": 0, "notes": 0, "sources": 0}
    try:
        target = MemoryService(staging)
        for manifest_path in sorted(source.glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text())
            if "notes" not in manifest or "version" in manifest:
                raise ValueError("source must use the original note-store format")
            domain = manifest["domain"]
            if manifest_path.parent.name != hashlib.sha256(domain.encode()).hexdigest():
                raise ValueError("source audience directory does not match its manifest")
            access = MemoryAccess((domain,), domain)
            counts["domains"] += 1
            for note_id, filename in manifest["notes"].items():
                if Path(filename).name != filename:
                    raise ValueError("source object name must not contain a path")
                path = manifest_path.parent / "objects" / filename
                if path.is_symlink():
                    raise ValueError("source objects must not be symbolic links")
                raw = path.read_bytes()
                header, body = raw.decode().removeprefix("---\n").split("\n---\n\n", 1)
                metadata = json.loads(header)
                if metadata["id"] != note_id:
                    raise ValueError("source object ID differs from its manifest")
                # Retain all original metadata, including applicability and merged
                # identities, as evidence rather than new current-state fields.
                document = (
                    "Imported legacy memory; not independently reverified.\n\n"
                    "Original metadata:\n```json\n"
                    + json.dumps(metadata, ensure_ascii=False, indent=2)
                    + "\n```\n\nOriginal content:\n"
                    + body
                )
                chunks = [document[start : start + 15_500] for start in range(0, len(document), 15_500)]
                for index, chunk in enumerate(chunks, start=1):
                    target.checkpoint_source(
                        access,
                        task_id=f"legacy:{note_id}:{index}",
                        input_digest=hashlib.sha256(raw + str(index).encode()).hexdigest(),
                        cursor=1,
                        complete=True,
                        title=metadata["title"],
                        body=(
                            f"Imported legacy memory {note_id}, part {index}/{len(chunks)}.\n\n"
                            f"{chunk}<!-- End of imported part -->"
                        ),
                        topics=[topic for topic in metadata["topics"] if len(topic) <= 128][:32],
                        sources=[*metadata["sources"], f"legacy-note:{note_id}"],
                        expected_revision=None,
                    )
                    counts["sources"] += 1
                counts["notes"] += 1
        staging.replace(destination)
        return counts
    finally:
        if staging.exists():
            shutil.rmtree(staging)
