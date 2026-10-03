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
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)
SOURCE_BODY_MAX_CHARS = 16_000
NAVIGATION_MAX_CHARS = 8_000
_ID = re.compile(r"[0-9a-f]{32}\Z")
_NAVIGATION_REFERENCE = re.compile(r"\bmemory:([^\s<>()\[\]`\"']+)")


class MemoryDenied(PermissionError):
    """The caller has no memory contribution domain."""


class MemoryNotFound(LookupError):
    """A source is absent from the caller's visible memory set."""


class MemoryConflict(ValueError):
    """A background result was computed from an outdated memory snapshot."""


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
    """An audience assigned by the service, never accepted from model output.

    read_domains grants recall. write_domain is the destination for the service's
    background producer; it does not grant the serving agent a memory write tool.
    Topics and repository paths do not alter these permissions.
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
class MemorySource:
    id: str
    domain: str
    task_id: str
    title: str
    body: str
    topics: tuple[str, ...]
    sources: tuple[str, ...]
    revision: str
    updated_at: str
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("topics", "sources"):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class MemorySourceCheckpoint(MemorySource):
    input_digest: str
    cursor: int


@dataclass(frozen=True)
class MemoryNavigation:
    id: str
    domain: str
    body: str
    sources: tuple[str, ...]
    source_revisions: dict[str, str]
    revision: str
    updated_at: str
    stale: bool = False

    @property
    def input_digest(self) -> str:
        return _source_digest(self.source_revisions)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "sources": list(self.sources), "input_digest": self.input_digest}


@dataclass(frozen=True)
class MemorySnapshot:
    domain: str
    sources: tuple[MemorySource, ...]
    navigation: MemoryNavigation | None

    @property
    def source_revisions(self) -> dict[str, str]:
        return {source.id: source.revision for source in self.sources}

    @property
    def input_digest(self) -> str:
        return _source_digest(self.source_revisions)


def _json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _source_digest(revisions: dict[str, str]) -> str:
    return hashlib.sha256(_json(revisions)).hexdigest()


def _identifier(kind: str, domain: str, task_id: str = "") -> str:
    return hashlib.sha256(_json([kind, domain, task_id])).hexdigest()[:32]


def _body(text: str, limit: int) -> str:
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("memory body must be text without NUL characters")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")
    if len(normalized) > limit:
        raise ValueError(f"memory body must be at most {limit} characters")
    return normalized


def _source_content(task_id: str, title: str, body: str, topics, sources) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip() or len(title) > 512 or any(c in title for c in "\r\n\x00"):
        raise ValueError("title must be nonempty single-line text of at most 512 characters")
    topics = _strings(topics, "topics", casefold=True)
    if len(topics) > 32 or any(len(topic) > 128 for topic in topics):
        raise ValueError("topics must contain at most 32 values of at most 128 characters")
    return {
        "title": unicodedata.normalize("NFC", title).strip(),
        "body": _body(body, SOURCE_BODY_MAX_CHARS),
        "topics": topics,
        "sources": tuple(sorted(set(_strings(sources, "sources")) | {f"task:{task_id}"})),
    }


def _task_id(task_id: str) -> str:
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or len(task_id) > 1024
        or any(c in task_id for c in "\r\n\x00")
    ):
        raise ValueError("task_id must be nonempty single-line text of at most 1024 characters")
    return task_id


def _input_digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise ValueError("input_digest must be nonempty text of at most 256 characters")
    return value


def _markdown(record: MemorySourceCheckpoint | MemoryNavigation) -> bytes:
    data = asdict(record)
    for field in ("domain", "revision", "body", "stale"):
        data.pop(field, None)
    data["kind"] = "source" if isinstance(record, MemorySourceCheckpoint) else "navigation"
    # JSON frontmatter is also valid YAML. Markdown remains the only body store.
    return ("---\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n---\n\n" + record.body + "\n").encode()


_Record = TypeVar("_Record", bound=MemorySourceCheckpoint | MemoryNavigation)


def _with_revision(record: _Record) -> _Record:
    return replace(record, revision=hashlib.sha256(_markdown(record)).hexdigest())


def _published(checkpoint: MemorySourceCheckpoint) -> MemorySource:
    return MemorySource(**{field.name: getattr(checkpoint, field.name) for field in fields(MemorySource)})


class MemoryService:
    """Audience-scoped source accounts and derived navigation.

    A domain manifest points to immutable Markdown objects. Source progress and
    its draft body commit together; only complete accounts enter recall. The
    last complete account stays visible while new evidence is being extracted.
    A completed empty account is retained as a receipt, but never recalled.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _directory(self, domain: str) -> Path:
        return self.root / hashlib.sha256(domain.encode()).hexdigest()

    def list_domains(self) -> tuple[str, ...]:
        """List the audiences present in committed memory manifests."""
        return tuple(sorted(json.loads(path.read_text())["domain"] for path in self.root.glob("*/manifest.json")))

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

    @staticmethod
    def _write_domain(access: MemoryAccess) -> str:
        if access.write_domain is None:
            raise MemoryDenied("this task has no memory contribution domain")
        return access.write_domain

    @staticmethod
    def _manifest(directory: Path, domain: str) -> dict[str, Any]:
        path = directory / "manifest.json"
        if not path.exists():
            return {"domain": domain, "sources": {}, "drafts": {}, "navigation": None}
        return json.loads(path.read_text())

    @staticmethod
    def _object(directory: Path, filename: str) -> tuple[dict[str, Any], str, str]:
        raw = (directory / "objects" / filename).read_bytes()
        header, body = raw.decode().removeprefix("---\n").split("\n---\n\n", 1)
        return json.loads(header), body.removesuffix("\n"), hashlib.sha256(raw).hexdigest()

    def _checkpoint(self, directory: Path, domain: str, filename: str) -> MemorySourceCheckpoint:
        data, body, revision = self._object(directory, filename)
        if data.pop("kind") != "source":
            raise ValueError("memory source references a non-source object")
        for field in ("topics", "sources"):
            data[field] = tuple(data[field])
        return MemorySourceCheckpoint(domain=domain, body=body, revision=revision, **data)

    def _sources(self, directory: Path, domain: str, manifest) -> tuple[MemorySource, ...]:
        checkpoints = [self._checkpoint(directory, domain, filename) for filename in manifest["sources"].values()]
        return tuple(
            sorted(
                (_published(source) for source in checkpoints if source.complete and source.body),
                key=lambda source: (source.title.casefold(), source.id),
            )
        )

    def _navigation(self, directory: Path, domain: str, manifest, sources) -> MemoryNavigation | None:
        filename = manifest["navigation"]
        if filename is None:
            return None
        data, body, revision = self._object(directory, filename)
        if data.pop("kind") != "navigation":
            raise ValueError("memory navigation references a non-navigation object")
        data["sources"] = tuple(data["sources"])
        navigation = MemoryNavigation(domain=domain, body=body, revision=revision, **data)
        current = {source.id: source.revision for source in sources}
        return replace(navigation, stale=navigation.source_revisions != current)

    def list_sources(self, access: MemoryAccess) -> list[MemorySource]:
        result = []
        for domain in access.read_domains:
            with self._locked(domain) as directory:
                result.extend(self._sources(directory, domain, self._manifest(directory, domain)))
        return sorted(result, key=lambda source: (source.title.casefold(), source.id))

    def read(self, access: MemoryAccess, source_id: str) -> MemorySource:
        if isinstance(source_id, str) and _ID.fullmatch(source_id):
            for source in self.list_sources(access):
                if source.id == source_id:
                    return source
        raise MemoryNotFound("memory source not found")

    def search(
        self, access: MemoryAccess, query: str, *, topics: Sequence[str] = (), limit: int = 10
    ) -> list[MemorySource]:
        if not isinstance(query, str) or len(query) > 8192:
            raise ValueError("query must be text of at most 8192 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        required_topics = set(_strings(topics, "topics", casefold=True))
        terms = set(re.findall(r"\w+", unicodedata.normalize("NFC", query).casefold()))
        matches = []
        for source in self.list_sources(access):
            if not required_topics.issubset(source.topics):
                continue
            values = ((source.task_id, 4), (source.title, 4), (" ".join(source.topics), 5), (source.body, 1))
            score = sum(weight for text, weight in values for term in terms if term in text.casefold())
            if score or not terms:
                matches.append((score, source))
        matches.sort(key=lambda item: (-item[0], item[1].title.casefold(), item[1].id))
        return [source for _, source in matches[:limit]]

    def list_navigation(self, access: MemoryAccess) -> list[MemoryNavigation]:
        result = []
        for domain in access.read_domains:
            with self._locked(domain) as directory:
                manifest = self._manifest(directory, domain)
                sources = self._sources(directory, domain, manifest)
                if navigation := self._navigation(directory, domain, manifest, sources):
                    result.append(navigation)
        return result

    def describe(self, access: MemoryAccess) -> dict[str, Any]:
        sources = self.list_sources(access)
        domains = Counter(source.domain for source in sources)
        topics = Counter(topic for source in sources for topic in source.topics)
        return {
            "count": len(sources),
            "domains": [{"id": domain, "count": domains[domain]} for domain in access.read_domains],
            "topics": [{"name": topic, "count": count} for topic, count in sorted(topics.items())],
            "navigation_count": len(self.list_navigation(access)),
        }

    def source_state(self, access: MemoryAccess, task_id: str) -> MemorySourceCheckpoint | None:
        domain = self._write_domain(access)
        source_id = _identifier("source", domain, _task_id(task_id))
        with self._locked(domain) as directory:
            manifest = self._manifest(directory, domain)
            filename = manifest["drafts"].get(source_id) or manifest["sources"].get(source_id)
            return self._checkpoint(directory, domain, filename) if filename else None

    def checkpoint_source(
        self,
        access: MemoryAccess,
        task_id: str,
        *,
        input_digest: str,
        cursor: int,
        complete: bool,
        title: str,
        body: str,
        topics: Sequence[str] = (),
        sources: Sequence[str] = (),
        expected_revision: str | None = None,
    ) -> MemorySourceCheckpoint:
        domain = self._write_domain(access)
        task_id = _task_id(task_id)
        input_digest = _input_digest(input_digest)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("cursor must be a nonnegative integer")
        if not isinstance(complete, bool):
            raise ValueError("complete must be a boolean")
        content: dict[str, Any] = {
            **_source_content(task_id, title, body, topics, sources),
            "input_digest": input_digest,
            "cursor": cursor,
            "complete": complete,
        }
        source_id = _identifier("source", domain, task_id)
        with self._locked(domain, write=True) as directory:
            manifest = self._manifest(directory, domain)
            filename = manifest["drafts"].get(source_id) or manifest["sources"].get(source_id)
            previous = self._checkpoint(directory, domain, filename) if filename else None
            if previous and all(getattr(previous, key) == value for key, value in content.items()):
                return previous
            self._check_revision(previous, expected_revision)
            if previous and previous.input_digest == input_digest:
                if previous.complete or cursor <= previous.cursor:
                    raise MemoryConflict("source input is already complete or its checkpoint did not advance")
            checkpoint = _with_revision(
                MemorySourceCheckpoint(
                    id=source_id,
                    domain=domain,
                    task_id=task_id,
                    revision="",
                    updated_at=datetime.now(UTC).isoformat(),
                    **content,
                )
            )
            filename = self._save_object(directory, checkpoint)
            if complete:
                manifest["sources"][source_id] = filename
                manifest["drafts"].pop(source_id, None)
            else:
                manifest["drafts"][source_id] = filename
            self._commit(directory, manifest)
            return checkpoint

    def snapshot_domain(self, access: MemoryAccess) -> MemorySnapshot:
        domain = self._write_domain(access)
        with self._locked(domain) as directory:
            manifest = self._manifest(directory, domain)
            sources = self._sources(directory, domain, manifest)
            return MemorySnapshot(domain, sources, self._navigation(directory, domain, manifest, sources))

    def publish_navigation(
        self,
        access: MemoryAccess,
        *,
        body: str,
        source_ids: Sequence[str],
        source_revisions: dict[str, str],
        input_digest: str,
        expected_revision: str | None = None,
        candidate_ids: Sequence[str] | None = None,
    ) -> MemoryNavigation:
        domain = self._write_domain(access)
        body = _body(body, NAVIGATION_MAX_CHARS)
        source_ids = _strings(source_ids, "source_ids")
        candidates = set(source_revisions if candidate_ids is None else _strings(candidate_ids, "candidate_ids"))
        if not set(source_ids).issubset(candidates) or not candidates.issubset(source_revisions):
            raise ValueError("navigation must reference only supplied source candidates")
        if any(not _ID.fullmatch(source_id) for source_id in source_ids):
            raise ValueError("navigation source IDs must identify memory sources")
        if not set(_NAVIGATION_REFERENCE.findall(body)).issubset(source_ids):
            raise ValueError("navigation contains an undeclared memory source reference")
        if _input_digest(input_digest) != _source_digest(source_revisions):
            raise ValueError("navigation input digest does not match its source revisions")
        with self._locked(domain, write=True) as directory:
            manifest = self._manifest(directory, domain)
            sources = self._sources(directory, domain, manifest)
            current = {source.id: source.revision for source in sources}
            if source_revisions != current:
                raise MemoryConflict("navigation sources changed; read a fresh domain snapshot")
            previous = self._navigation(directory, domain, manifest, sources)
            if (
                previous
                and previous.body == body
                and previous.sources == source_ids
                and previous.source_revisions == source_revisions
            ):
                return previous
            self._check_revision(previous, expected_revision)
            navigation = _with_revision(
                MemoryNavigation(
                    id=_identifier("navigation", domain),
                    domain=domain,
                    body=body,
                    sources=source_ids,
                    source_revisions=dict(source_revisions),
                    revision="",
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            manifest["navigation"] = self._save_object(directory, navigation)
            self._commit(directory, manifest)
            return navigation

    @staticmethod
    def _check_revision(previous, expected_revision: str | None) -> None:
        current = previous.revision if previous else None
        if expected_revision != current:
            raise MemoryConflict("memory changed; read its current revision")

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

    def _save_object(self, directory: Path, record: MemorySourceCheckpoint | MemoryNavigation) -> str:
        objects = directory / "objects"
        objects.mkdir(exist_ok=True, mode=0o700)
        filename = f"{record.id}-{record.revision}.md"
        path = objects / filename
        if not path.exists():
            self._atomic_write(path, _markdown(record))
        return filename

    def _commit(self, directory: Path, manifest) -> None:
        self._atomic_write(directory / "manifest.json", _json(manifest))
        # The manifest is the commit boundary. Garbage collection cannot turn a
        # successful publication into a failed job or make orphan files visible.
        live = set(manifest["sources"].values()) | set(manifest["drafts"].values()) | {manifest["navigation"]}
        try:
            for path in (directory / "objects").glob("*.md"):
                if path.name not in live:
                    path.unlink()
        except OSError:
            logger.warning("Memory committed, but obsolete object cleanup failed", exc_info=True)
