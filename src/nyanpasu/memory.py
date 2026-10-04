from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from nyanpasu.memory_search import bm25_scores, tokenize

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)
SOURCE_BODY_MAX_CHARS = 16_000
SUMMARY_MAX_BYTES = 1_024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SUMMARY_REFERENCE = re.compile(r"\bmemory:([^\s<>()\[\]`\"']+)")


class MemoryDenied(PermissionError):
    """The caller has no memory contribution domain."""


class MemoryNotFound(LookupError):
    """A source is absent from the caller's visible memory set."""


class MemoryConflict(ValueError):
    """A background result was computed from an outdated memory snapshot."""


class SummaryTooLarge(ValueError):
    """The complete rendered summary exceeds its injection budget."""


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
    context_key: str
    context_generation: int
    source_order: float
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
    task_digests: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MemorySummary:
    id: str
    domain: str
    context_key: str
    context_generation: int
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
    context_key: str
    context_generation: int
    sources: tuple[MemorySource, ...]
    summary: MemorySummary | None

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


def _source_id(domain: str, context_key: str, context_generation: int) -> str:
    return hashlib.sha256(_json(["source", domain, context_key, context_generation])).hexdigest()[:32]


def _summary_id(domain: str, context_key: str, context_generation: int) -> str:
    return hashlib.sha256(_json(["summary", domain, context_key, context_generation])).hexdigest()[:32]


def _summary_block(summary_id: str, body: str) -> str:
    return json.dumps({"id": summary_id, "body": body}, ensure_ascii=False, separators=(",", ":"))


def validate_summary_block(summary_id: str, body: str) -> None:
    size = len(_summary_block(summary_id, body).encode("utf-8"))
    if size > SUMMARY_MAX_BYTES:
        raise SummaryTooLarge(f"rendered memory summary is {size} bytes; maximum is {SUMMARY_MAX_BYTES} bytes")


def summary_block(summary: MemorySummary) -> str:
    return _summary_block(summary.id, summary.body)


def _context(context_key: str, context_generation: int) -> tuple[str, int]:
    if not isinstance(context_key, str) or not context_key.strip() or "\x00" in context_key:
        raise ValueError("context_key must be nonempty text without NUL")
    if not isinstance(context_generation, int) or isinstance(context_generation, bool) or context_generation < 1:
        raise ValueError("context_generation must be a positive integer")
    return context_key, context_generation


def _source_order(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("source_order must be a finite nonnegative number")
    return value


def _body(text: str, limit: int | None = None) -> str:
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("memory body must be text without NUL characters")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")
    if limit is not None and len(normalized) > limit:
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


def _markdown(record: MemorySourceCheckpoint | MemorySummary) -> bytes:
    data = asdict(record)
    for name in ("domain", "revision", "body", "stale"):
        data.pop(name, None)
    data["kind"] = "source" if isinstance(record, MemorySourceCheckpoint) else "summary"
    # JSON frontmatter is also valid YAML. Markdown remains the only body store.
    return ("---\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n---\n\n" + record.body + "\n").encode()


_Record = TypeVar("_Record", bound=MemorySourceCheckpoint | MemorySummary)


def _with_revision(record: _Record) -> _Record:
    return replace(record, revision=hashlib.sha256(_markdown(record)).hexdigest())


def _published(checkpoint: MemorySourceCheckpoint) -> MemorySource:
    return MemorySource(**{field.name: getattr(checkpoint, field.name) for field in fields(MemorySource)})


class MemoryService:
    """One rolling account and derived summary per audience and context generation.

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
            return {"domain": domain, "sources": {}, "drafts": {}, "summaries": {}}
        manifest = json.loads(path.read_text())
        manifest.setdefault("summaries", {})
        manifest.setdefault("aliases", {})
        return manifest

    @staticmethod
    def _object(directory: Path, filename: str) -> tuple[dict[str, Any], str, str]:
        raw = (directory / "objects" / filename).read_bytes()
        header, body = raw.decode().removeprefix("---\n").split("\n---\n\n", 1)
        return json.loads(header), body.removesuffix("\n"), hashlib.sha256(raw).hexdigest()

    def _checkpoint(self, directory: Path, domain: str, filename: str) -> MemorySourceCheckpoint:
        data, body, revision = self._object(directory, filename)
        if data.pop("kind") != "source":
            raise ValueError("memory source references a non-source object")
        data.setdefault("context_key", data["task_id"])
        data.setdefault("context_generation", 1)
        data.setdefault("source_order", 0)
        data.setdefault("task_digests", {data["task_id"]: data["input_digest"]} if data["complete"] else {})
        for name in ("topics", "sources"):
            data[name] = tuple(data[name])
        return MemorySourceCheckpoint(domain=domain, body=body, revision=revision, **data)

    def _sources(
        self, directory: Path, domain: str, manifest, *, include_empty: bool = False
    ) -> tuple[MemorySource, ...]:
        checkpoints = [self._checkpoint(directory, domain, filename) for filename in manifest["sources"].values()]
        return tuple(
            sorted(
                (_published(source) for source in checkpoints if source.complete and (include_empty or source.body)),
                key=lambda source: (source.source_order, source.task_id),
            )
        )

    def _summary(self, directory: Path, domain: str, filename: str, sources) -> MemorySummary:
        data, body, revision = self._object(directory, filename)
        if data.pop("kind") != "summary":
            raise ValueError("memory summary references a non-summary object")
        data["sources"] = tuple(data["sources"])
        summary = MemorySummary(domain=domain, body=body, revision=revision, **data)
        current = {
            source.id: source.revision
            for source in sources
            if (source.context_key, source.context_generation) == (summary.context_key, summary.context_generation)
        }
        return replace(summary, stale=summary.source_revisions != current)

    def list_sources(self, access: MemoryAccess, *, include_empty: bool = False) -> list[MemorySource]:
        result = []
        for domain in access.read_domains:
            with self._locked(domain) as directory:
                result.extend(
                    self._sources(directory, domain, self._manifest(directory, domain), include_empty=include_empty)
                )
        return sorted(result, key=lambda source: (source.title.casefold(), source.id))

    def read(self, access: MemoryAccess, source_id: str) -> MemorySource:
        if isinstance(source_id, str) and _ID.fullmatch(source_id):
            for domain in access.read_domains:
                with self._locked(domain) as directory:
                    manifest = self._manifest(directory, domain)
                    identity = manifest.get("aliases", {}).get(source_id, source_id)
                    filename = manifest["sources"].get(identity)
                    if filename:
                        source = self._checkpoint(directory, domain, filename)
                        if source.complete and source.body:
                            return _published(source)
        raise MemoryNotFound("memory source not found")

    def search(
        self, access: MemoryAccess, query: str, *, topics: Sequence[str] = (), limit: int = 10
    ) -> list[MemorySource]:
        if not isinstance(query, str) or len(query) > 8192:
            raise ValueError("query must be text of at most 8192 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        required_topics = set(_strings(topics, "topics", casefold=True))
        sources = [source for source in self.list_sources(access) if required_topics.issubset(source.topics)]
        if not query.strip():
            return sources[:limit]

        # Exact context/task lookup also resolves older tasks in a rolling account.
        # Provenance must not inflate document length or keyword frequency.
        identity = unicodedata.normalize("NFC", query).strip().casefold()
        exact = [
            source
            for source in sources
            if identity == source.context_key.casefold()
            or any(
                identity == reference.removeprefix("task:").casefold()
                for reference in source.sources
                if reference.startswith("task:")
            )
        ]
        if exact:
            return exact[:limit]

        scores = bm25_scores(
            set(tokenize(query)),
            [
                (
                    (source.context_key, 4.0),
                    (source.title, 4.0),
                    (" ".join(source.topics), 5.0),
                    (source.body, 1.0),
                )
                for source in sources
            ],
            prefix=True,
        )
        matches = [(score, source) for score, source in zip(scores, sources, strict=True) if score > 0]
        matches.sort(key=lambda item: (-item[0], item[1].title.casefold(), item[1].id))
        return [source for _, source in matches[:limit]]

    def list_summaries(self, access: MemoryAccess) -> list[MemorySummary]:
        result = []
        for domain in access.read_domains:
            with self._locked(domain) as directory:
                manifest = self._manifest(directory, domain)
                sources = self._sources(directory, domain, manifest)
                result.extend(
                    self._summary(directory, domain, filename, sources) for filename in manifest["summaries"].values()
                )
        return sorted(result, key=lambda item: (item.domain, item.context_key, item.context_generation))

    def describe(self, access: MemoryAccess) -> dict[str, Any]:
        sources = self.list_sources(access)
        domains = Counter(source.domain for source in sources)
        topics = Counter(topic for source in sources for topic in source.topics)
        return {
            "count": len(sources),
            "domains": [{"id": domain, "count": domains[domain]} for domain in access.read_domains],
            "topics": [{"name": topic, "count": count} for topic, count in sorted(topics.items())],
            "summary_count": len(self.list_summaries(access)),
        }

    def source_state(
        self, access: MemoryAccess, context_key: str, context_generation: int = 1
    ) -> MemorySourceCheckpoint | None:
        return self._source_state(access, context_key, context_generation, include_draft=True)

    def published_source_state(
        self, access: MemoryAccess, context_key: str, context_generation: int = 1
    ) -> MemorySourceCheckpoint | None:
        return self._source_state(access, context_key, context_generation, include_draft=False)

    def _source_state(
        self, access: MemoryAccess, context_key: str, context_generation: int, *, include_draft: bool
    ) -> MemorySourceCheckpoint | None:
        domain = self._write_domain(access)
        context_key, context_generation = _context(context_key, context_generation)
        source_id = _source_id(domain, context_key, context_generation)
        with self._locked(domain) as directory:
            manifest = self._manifest(directory, domain)
            filename = manifest["drafts"].get(source_id) if include_draft else None
            filename = filename or manifest["sources"].get(source_id)
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
        context_key: str | None = None,
        context_generation: int = 1,
        source_order: float = 0,
        topics: Sequence[str] = (),
        sources: Sequence[str] = (),
        expected_revision: str | None = None,
    ) -> MemorySourceCheckpoint:
        domain = self._write_domain(access)
        task_id = _task_id(task_id)
        context_key, context_generation = _context(task_id if context_key is None else context_key, context_generation)
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
            "context_key": context_key,
            "context_generation": context_generation,
            "source_order": _source_order(source_order),
        }
        source_id = _source_id(domain, context_key, context_generation)
        with self._locked(domain, write=True) as directory:
            manifest = self._manifest(directory, domain)
            filename = manifest["drafts"].get(source_id) or manifest["sources"].get(source_id)
            previous = self._checkpoint(directory, domain, filename) if filename else None
            published_filename = manifest["sources"].get(source_id)
            published = self._checkpoint(directory, domain, published_filename) if published_filename else None
            content["source_order"] = max(
                content["source_order"],
                previous.source_order if previous else 0,
                published.source_order if published else 0,
            )
            content["sources"] = _strings(
                (
                    *content["sources"],
                    *(previous.sources if previous else ()),
                    *(published.sources if published else ()),
                ),
                "sources",
            )
            task_digests = dict(previous.task_digests) if previous else {}
            if complete:
                task_digests[task_id] = input_digest
            else:
                task_digests.pop(task_id, None)
            content["task_digests"] = task_digests
            if (
                previous
                and previous.task_id == task_id
                and all(getattr(previous, key) == value for key, value in content.items())
            ):
                return previous
            self._check_revision(previous, expected_revision)
            if previous:
                if not previous.complete and previous.task_id != task_id:
                    raise MemoryConflict("finish the pending source task before updating this context")
                if previous.task_digests.get(task_id) == input_digest:
                    raise MemoryConflict("source task input is already complete")
                if previous.task_id == task_id and previous.input_digest == input_digest and cursor <= previous.cursor:
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

    def bind_source_contexts(self, access: MemoryAccess, mapping: dict[str, tuple[str, int, float]]) -> int:
        """Merge legacy task accounts without discarding their text or evidence links."""
        domain = self._write_domain(access)
        with self._locked(domain, write=True) as directory:
            manifest = self._manifest(directory, domain)
            groups: dict[str, list[MemorySourceCheckpoint]] = {}
            changed: set[str] = set()
            for identity, filename in manifest["sources"].items():
                data, _, _ = self._object(directory, filename)
                checkpoint = self._checkpoint(directory, domain, filename)
                if "context_key" not in data:
                    if checkpoint.task_id not in mapping:
                        continue
                    key, generation, order = mapping[checkpoint.task_id]
                    key, generation = _context(key, generation)
                    checkpoint = replace(
                        checkpoint,
                        context_key=key,
                        context_generation=generation,
                        source_order=_source_order(order),
                    )
                target = _source_id(domain, checkpoint.context_key, checkpoint.context_generation)
                groups.setdefault(target, []).append(checkpoint)
                if identity != target or "task_digests" not in data:
                    changed.add(checkpoint.task_id)

            aliases = manifest.setdefault("aliases", {})
            for target, checkpoints in groups.items():
                if not any(checkpoint.task_id in changed for checkpoint in checkpoints):
                    continue
                ordered = sorted(checkpoints, key=lambda checkpoint: (checkpoint.source_order, checkpoint.task_id))
                latest = ordered[-1]
                retained = [checkpoint for checkpoint in ordered if checkpoint.body]
                body = (
                    retained[0].body
                    if len(retained) == 1
                    else "\n\n".join(f"## {checkpoint.title}\n\n{checkpoint.body}" for checkpoint in retained)
                )
                merged = _with_revision(
                    replace(
                        latest,
                        id=target,
                        body=body,
                        topics=_strings(
                            tuple(topic for checkpoint in ordered for topic in checkpoint.topics), "topics"
                        ),
                        sources=_strings(
                            tuple(source for checkpoint in ordered for source in checkpoint.sources), "sources"
                        ),
                        task_digests={
                            task_id: digest
                            for checkpoint in ordered
                            for task_id, digest in checkpoint.task_digests.items()
                        },
                    )
                )
                for checkpoint in ordered:
                    manifest["sources"].pop(checkpoint.id)
                    if checkpoint.id != target:
                        aliases[checkpoint.id] = target
                manifest["sources"][target] = self._save_object(directory, merged)

            # A legacy draft describes just one task, not the whole rolling account.
            # Leave it unprocessed so maintenance restarts from the published history.
            for identity, filename in tuple(manifest["drafts"].items()):
                data, _, _ = self._object(directory, filename)
                if "task_digests" not in data and data["task_id"] in mapping:
                    manifest["drafts"].pop(identity)
                    changed.add(data["task_id"])
                    key, generation, _ = mapping[data["task_id"]]
                    target = _source_id(
                        domain, data.get("context_key", key), data.get("context_generation", generation)
                    )
                    published_filename = manifest["sources"].get(target)
                    if published_filename:
                        published = self._checkpoint(directory, domain, published_filename)
                        task_digests = dict(published.task_digests)
                        if task_digests.pop(data["task_id"], None) is not None:
                            retryable = _with_revision(replace(published, task_digests=task_digests))
                            manifest["sources"][target] = self._save_object(directory, retryable)
            if changed or "navigation" in manifest:
                self._commit(directory, manifest)
        return len(changed)

    def snapshot_context(self, access: MemoryAccess, context_key: str, context_generation: int) -> MemorySnapshot:
        domain = self._write_domain(access)
        context_key, context_generation = _context(context_key, context_generation)
        with self._locked(domain) as directory:
            manifest = self._manifest(directory, domain)
            sources = tuple(
                source
                for source in self._sources(directory, domain, manifest)
                if (source.context_key, source.context_generation) == (context_key, context_generation)
            )
            filename = manifest["summaries"].get(_summary_id(domain, context_key, context_generation))
            summary = self._summary(directory, domain, filename, sources) if filename else None
            return MemorySnapshot(domain, context_key, context_generation, sources, summary)

    def publish_summary(
        self,
        access: MemoryAccess,
        context_key: str,
        context_generation: int,
        *,
        body: str,
        source_ids: Sequence[str],
        source_revisions: dict[str, str],
        input_digest: str,
        expected_revision: str | None = None,
        candidate_ids: Sequence[str] | None = None,
    ) -> MemorySummary:
        domain = self._write_domain(access)
        context_key, context_generation = _context(context_key, context_generation)
        identity = _summary_id(domain, context_key, context_generation)
        body = _body(body)
        validate_summary_block(identity, body)
        source_ids = _strings(source_ids, "source_ids")
        candidates = set(source_revisions if candidate_ids is None else _strings(candidate_ids, "candidate_ids"))
        if not set(source_ids).issubset(candidates) or not candidates.issubset(source_revisions):
            raise ValueError("summary must reference only supplied source candidates")
        if any(not _ID.fullmatch(source_id) for source_id in source_ids):
            raise ValueError("summary source IDs must identify memory sources")
        if not set(_SUMMARY_REFERENCE.findall(body)).issubset(source_ids):
            raise ValueError("summary contains an undeclared memory source reference")
        if _input_digest(input_digest) != _source_digest(source_revisions):
            raise ValueError("summary input digest does not match its source revisions")
        with self._locked(domain, write=True) as directory:
            manifest = self._manifest(directory, domain)
            sources = tuple(
                source
                for source in self._sources(directory, domain, manifest)
                if (source.context_key, source.context_generation) == (context_key, context_generation)
            )
            current = {source.id: source.revision for source in sources}
            if source_revisions != current:
                raise MemoryConflict("summary sources changed; read a fresh context snapshot")
            filename = manifest["summaries"].get(identity)
            previous = self._summary(directory, domain, filename, sources) if filename else None
            if (
                previous
                and previous.body == body
                and previous.sources == source_ids
                and previous.source_revisions == source_revisions
            ):
                return previous
            self._check_revision(previous, expected_revision)
            summary = _with_revision(
                MemorySummary(
                    id=identity,
                    domain=domain,
                    context_key=context_key,
                    context_generation=context_generation,
                    body=body,
                    sources=source_ids,
                    source_revisions=dict(source_revisions),
                    revision="",
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            manifest["summaries"][identity] = self._save_object(directory, summary)
            self._commit(directory, manifest)
            return summary

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

    def _save_object(self, directory: Path, record: MemorySourceCheckpoint | MemorySummary) -> str:
        objects = directory / "objects"
        objects.mkdir(exist_ok=True, mode=0o700)
        filename = f"{record.id}-{record.revision}.md"
        path = objects / filename
        if not path.exists():
            self._atomic_write(path, _markdown(record))
        return filename

    def _commit(self, directory: Path, manifest) -> None:
        manifest.pop("navigation", None)
        self._atomic_write(directory / "manifest.json", _json(manifest))
        # The manifest is the commit boundary. Garbage collection cannot turn a
        # successful publication into a failed job or make orphan files visible.
        live = (
            set(manifest["sources"].values()) | set(manifest["drafts"].values()) | set(manifest["summaries"].values())
        )
        try:
            for path in (directory / "objects").glob("*.md"):
                if path.name not in live:
                    path.unlink()
        except OSError:
            logger.warning("Memory committed, but obsolete object cleanup failed", exc_info=True)
