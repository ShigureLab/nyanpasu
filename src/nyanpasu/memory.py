from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)
_KEY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")


class MemoryDenied(PermissionError):
    """The task has no memory write capability."""


class MemoryNotFound(LookupError):
    """A note is absent from the task's visible memory set."""


class MemoryConflict(ValueError):
    """A write needs a fresh revision or an explicit merge."""


def _strings(values: Sequence[str], field: str, *, casefold: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a list of strings")
    normalized = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
            raise ValueError(f"{field} must contain nonempty strings of at most 4096 characters")
        value = unicodedata.normalize("NFC", value).strip()
        normalized.add(value.casefold() if casefold else value)
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class MemoryAccess:
    """A capability assigned by the service, never accepted from a model request.

    Domains describe audiences, not topics or project paths. The empty capability
    disables memory. Reading public information never grants public write access.
    """

    read_domains: tuple[str, ...] = ()
    write_domain: str | None = None

    def __post_init__(self) -> None:
        domains = _strings(self.read_domains, "read_domains")
        object.__setattr__(self, "read_domains", domains)
        if self.write_domain is not None and self.write_domain not in domains:
            raise ValueError("write_domain must be one of read_domains")

    def to_dict(self) -> dict[str, Any]:
        return {"read_domains": list(self.read_domains), "write_domain": self.write_domain}


@dataclass(frozen=True)
class MemoryNote:
    id: str
    domain: str
    key: str
    title: str
    body: str
    topics: tuple[str, ...]
    applies_to: tuple[str, ...]
    sources: tuple[str, ...]
    revision: str
    updated_at: str
    merged_from: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("topics", "applies_to", "sources", "merged_from"):
            data[key] = list(data[key])
        return data


def _body(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 524288 or "\x00" in text:
        raise ValueError("body must be nonempty text of at most 524288 characters")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")


def _content(key: str, title: str, body: str, topics, applies_to, sources) -> dict[str, Any]:
    if not isinstance(key, str) or len(key) > 160 or not _KEY.fullmatch(key):
        raise ValueError("key must be a lowercase slug of at most 160 characters (letters, digits, '.', '_', '-')")
    if not isinstance(title, str) or not title.strip() or len(title) > 512 or "\n" in title or "\x00" in title:
        raise ValueError("title must be one nonempty line of at most 512 characters")
    evidence = _strings(sources, "sources")
    if not evidence:
        raise ValueError("at least one source is required")
    return {
        "key": key,
        "title": unicodedata.normalize("NFC", title).strip(),
        "body": _body(body),
        "topics": _strings(topics, "topics", casefold=True),
        "applies_to": _strings(applies_to, "applies_to"),
        "sources": evidence,
    }


def _json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _signature(note: MemoryNote | dict[str, Any]) -> str:
    data = note.to_dict() if isinstance(note, MemoryNote) else note
    return hashlib.sha256(_json([data["body"], data["applies_to"]])).hexdigest()


def _markdown(note: MemoryNote) -> bytes:
    data = note.to_dict()
    for field in ("domain", "revision", "body"):
        data.pop(field)
    # JSON frontmatter is also valid YAML; no extra parser/runtime is required.
    return ("---\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n---\n\n" + note.body + "\n").encode()


def _new_note(
    domain: str, content: dict[str, Any], *, previous: MemoryNote | None = None, merged_from=()
) -> MemoryNote:
    merged_from = tuple(sorted(set(merged_from)))
    if previous and all(getattr(previous, key) == value for key, value in content.items()):
        if previous.merged_from == merged_from:
            return previous
    note = MemoryNote(
        id=previous.id if previous else uuid.uuid4().hex,
        domain=domain,
        revision="",
        updated_at=datetime.now(UTC).isoformat(),
        merged_from=merged_from,
        **content,
    )
    return replace(note, revision=hashlib.sha256(_markdown(note)).hexdigest())


class MemoryService:
    """Markdown notes with audience-first lookup and atomic, single-domain commits.

    Each domain's manifest names its live immutable Markdown files. Switching
    that one manifest commits a merge atomically. Unreferenced versions are
    garbage, never search results. index.md is disposable and can be rebuilt.
    All online writers use this service; flock also serializes other processes.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _directory(self, domain: str) -> Path:
        return self.root / hashlib.sha256(domain.encode()).hexdigest()

    @contextlib.contextmanager
    def _locked(self, domain: str, *, write: bool = False) -> Iterator[Path]:
        directory = self._directory(domain)
        if write:
            directory.mkdir(mode=0o700, exist_ok=True)
        elif not (directory / "lock").exists():
            yield directory
            return
        with (directory / "lock").open("a+b") as lock:
            Path(lock.name).chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            try:
                yield directory
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self, directory: Path, domain: str) -> tuple[dict[str, Any], dict[str, MemoryNote]]:
        if not (directory / "manifest.json").exists():
            return {"domain": domain, "notes": {}, "requests": {}}, {}
        manifest = json.loads((directory / "manifest.json").read_text())
        notes = {}
        for note_id, filename in manifest["notes"].items():
            raw = (directory / "objects" / filename).read_bytes()
            header, body = raw.decode().removeprefix("---\n").split("\n---\n\n", 1)
            data = json.loads(header)
            for field in ("topics", "applies_to", "sources", "merged_from"):
                data[field] = tuple(data[field])
            note = MemoryNote(domain=domain, body=body.removesuffix("\n"), revision="", **data)
            notes[note_id] = replace(note, revision=hashlib.sha256(_markdown(note)).hexdigest())
        return manifest, notes

    @staticmethod
    def _write_domain(access: MemoryAccess) -> str:
        if access.write_domain is None:
            raise MemoryDenied("this task cannot write memory")
        return access.write_domain

    def list_notes(self, access: MemoryAccess) -> list[MemoryNote]:
        result = []
        for domain in access.read_domains:
            with self._locked(domain) as directory:
                _, notes = self._load(directory, domain)
                result.extend(notes.values())
        return sorted(result, key=lambda note: (note.title.casefold(), note.id))

    def read(self, access: MemoryAccess, note_id: str) -> MemoryNote:
        if not isinstance(note_id, str) or not _ID.fullmatch(note_id):
            raise MemoryNotFound("memory not found")
        for note in self.list_notes(access):
            if note.id == note_id:
                return note
        raise MemoryNotFound("memory not found")

    def search(
        self, access: MemoryAccess, query: str, *, topics: Sequence[str] = (), limit: int = 10
    ) -> list[MemoryNote]:
        if not isinstance(query, str) or len(query) > 8192:
            raise ValueError("query must be text of at most 8192 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        required_topics = set(_strings(topics, "topics", casefold=True))
        terms = set(re.findall(r"\w+", unicodedata.normalize("NFC", query).casefold()))
        matches = []
        for note in self.list_notes(access):
            if not required_topics.issubset(note.topics):
                continue
            fields = (
                (note.key, 4),
                (note.title, 4),
                (" ".join(note.topics), 5),
                (note.body, 1),
                (" ".join(note.applies_to), 2),
            )
            score = sum(weight for text, weight in fields for term in terms if term in text.casefold())
            if score or not terms:
                matches.append((score, note))
        matches.sort(key=lambda item: (-item[0], item[1].title.casefold(), item[1].id))
        return [note for _, note in matches[:limit]]

    def describe(self, access: MemoryAccess) -> dict[str, Any]:
        notes = self.list_notes(access)
        domains = Counter(note.domain for note in notes)
        topics = Counter(topic for note in notes for topic in note.topics)
        return {
            "count": len(notes),
            "domains": [{"id": domain, "count": domains[domain]} for domain in access.read_domains],
            "topics": [{"name": topic, "count": count} for topic, count in sorted(topics.items())],
        }

    @staticmethod
    def _replay(manifest, notes, request_key, fingerprint) -> MemoryNote | None:
        if request_key is None:
            return None
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 256:
            raise ValueError("request_key must be nonempty text of at most 256 characters")
        receipt = manifest["requests"].get(request_key)
        if receipt is None:
            return None
        if receipt["fingerprint"] != fingerprint:
            raise MemoryConflict("request_key was already used with different input")
        if receipt["note_id"] not in notes:
            raise MemoryConflict("this request already succeeded, but its result was subsequently removed")
        return notes[receipt["note_id"]]

    @staticmethod
    def _check_revision(note: MemoryNote, expected_revision: str | None) -> None:
        if expected_revision is None or note.revision != expected_revision:
            raise MemoryConflict(f"memory {note.id} changed or expected_revision was missing; read it again")

    def write(
        self,
        access: MemoryAccess,
        *,
        key: str,
        title: str,
        body: str,
        topics: Sequence[str] = (),
        applies_to: Sequence[str] = (),
        sources: Sequence[str] = (),
        note_id: str | None = None,
        expected_revision: str | None = None,
        request_key: str | None = None,
    ) -> MemoryNote:
        domain = self._write_domain(access)
        content = _content(key, title, body, topics, applies_to, sources)
        fingerprint = hashlib.sha256(_json(["write", content, note_id, expected_revision])).hexdigest()
        with self._locked(domain, write=True) as directory:
            manifest, notes = self._load(directory, domain)
            if result := self._replay(manifest, notes, request_key, fingerprint):
                return result
            previous = None
            if note_id is not None:
                previous = notes.get(note_id)
                if previous is None:
                    raise MemoryNotFound("memory not found")
                self._check_revision(previous, expected_revision)
            elif expected_revision is not None:
                raise ValueError("expected_revision requires note_id")
            keyed = next((note for note in notes.values() if note.key == key and note.id != note_id), None)
            duplicate = next(
                (note for note in notes.values() if _signature(note) == _signature(content) and note.id != note_id),
                None,
            )
            if keyed is not None and keyed != duplicate:
                raise MemoryConflict(f"key already exists as memory {keyed.id}; read it and update with its revision")
            if duplicate:
                if previous:
                    raise MemoryConflict(f"content already exists as memory {duplicate.id}; use memory.merge")
                previous = duplicate
                content.update(key=duplicate.key, title=duplicate.title)
            if previous:
                content["sources"] = tuple(sorted(set(previous.sources) | set(content["sources"])))
                if duplicate:
                    content["topics"] = tuple(sorted(set(previous.topics) | set(content["topics"])))
            note = _new_note(domain, content, previous=previous, merged_from=previous.merged_from if previous else ())
            notes[note.id] = note
            self._finish(directory, manifest, notes, note, request_key, fingerprint)
            return note

    def delete(self, access: MemoryAccess, note_id: str, *, expected_revision: str) -> None:
        domain = self._write_domain(access)
        with self._locked(domain, write=True) as directory:
            manifest, notes = self._load(directory, domain)
            note = notes.get(note_id)
            if note is None:
                raise MemoryNotFound("memory not found")
            self._check_revision(note, expected_revision)
            del notes[note_id]
            self._commit(directory, manifest, notes)

    def merge(
        self,
        access: MemoryAccess,
        source_ids: Sequence[str],
        *,
        target_id: str,
        key: str,
        title: str,
        body: str,
        topics: Sequence[str] = (),
        applies_to: Sequence[str] = (),
        sources: Sequence[str] = (),
        expected_revisions: dict[str, str],
        request_key: str | None = None,
    ) -> MemoryNote:
        domain = self._write_domain(access)
        content = _content(key, title, body, topics, applies_to, sources)
        ids = set(_strings(source_ids, "source_ids")) | {target_id}
        if len(ids) < 2:
            raise ValueError("merge requires a target and at least one other note")
        if set(expected_revisions) != ids:
            raise ValueError("expected_revisions must cover exactly the target and source notes")
        fingerprint = hashlib.sha256(_json(["merge", sorted(ids), target_id, content, expected_revisions])).hexdigest()
        with self._locked(domain, write=True) as directory:
            manifest, notes = self._load(directory, domain)
            if result := self._replay(manifest, notes, request_key, fingerprint):
                return result
            if not ids.issubset(notes):
                raise MemoryNotFound("memory not found")
            for note_id in ids:
                self._check_revision(notes[note_id], expected_revisions[note_id])
            for other in notes.values():
                if other.id not in ids and (other.key == key or _signature(other) == _signature(content)):
                    raise MemoryConflict(f"merged content conflicts with memory {other.id}; include it explicitly")
            inputs = [notes[note_id] for note_id in sorted(ids)]
            content["sources"] = tuple(sorted(set(content["sources"]).union(*(note.sources for note in inputs))))
            content["topics"] = tuple(sorted(set(content["topics"]).union(*(note.topics for note in inputs))))
            retired = (ids - {target_id}).union(*(note.merged_from for note in inputs))
            note = _new_note(domain, content, previous=notes[target_id], merged_from=retired)
            for note_id in ids:
                del notes[note_id]
            notes[note.id] = note
            self._finish(directory, manifest, notes, note, request_key, fingerprint)
            return note

    def _finish(self, directory, manifest, notes, note, request_key, fingerprint) -> None:
        if request_key is not None:
            manifest["requests"][request_key] = {"fingerprint": fingerprint, "note_id": note.id}
        self._commit(directory, manifest, notes)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _commit(self, directory: Path, manifest, notes: dict[str, MemoryNote]) -> None:
        objects = directory / "objects"
        objects.mkdir(exist_ok=True, mode=0o700)
        filenames = {}
        for note in notes.values():
            filename = f"{note.id}-{note.revision}.md"
            path = objects / filename
            if not path.exists():
                self._atomic_write(path, _markdown(note))
            filenames[note.id] = filename
        manifest["notes"] = filenames
        self._atomic_write(directory / "manifest.json", _json(manifest))
        # Only the manifest is a commit boundary. Index/garbage maintenance must
        # not turn an already successful write into an apparent failed write.
        try:
            self._index(directory, notes)
            live = set(filenames.values())
            for path in objects.glob("*.md"):
                if path.name not in live:
                    path.unlink()
        except OSError:
            logger.warning("Memory was committed, but derived index/garbage maintenance failed", exc_info=True)

    def _index(self, directory: Path, notes: dict[str, MemoryNote]) -> None:
        lines = [
            "# Memory index",
            "",
            "Generated from active Markdown notes. Rebuild instead of editing this file.",
            "",
        ]
        for note in sorted(notes.values(), key=lambda item: item.key):
            title = note.title.replace("[", "\\[").replace("]", "\\]")
            lines.append(
                f"- [{title}](objects/{note.id}-{note.revision}.md) — `{note.key}`; topics: {', '.join(note.topics)}"
            )
        self._atomic_write(directory / "index.md", ("\n".join(lines) + "\n").encode())

    def rebuild_index(self, access: MemoryAccess) -> None:
        domain = self._write_domain(access)
        with self._locked(domain, write=True) as directory:
            _, notes = self._load(directory, domain)
            self._index(directory, notes)
