from __future__ import annotations

import json
import re
import stat
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from heapq import nlargest
from typing import TYPE_CHECKING, Any

import anyio.to_thread as to_thread
from anyio import Lock

from nyanpasu.environment import claude_projects, codex_home, process_env
from nyanpasu.redaction import redact
from nyanpasu.safe_files import open_regular_file
from nyanpasu.transcript.codex import open_rollout, zstd
from nyanpasu.transcript.history import session_key
from nyanpasu.transcript.models import (
    ModelUsageByAgent,
    ModelUsageByModel,
    ModelUsageBySession,
    ModelUsagePeriods,
    ModelUsageSnapshot,
    ModelUsageTotals,
    ModelUsageWindow,
)
from nyanpasu.transcript.pricing import estimate_api_cost

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from os import stat_result
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.transcript.queries import TranscriptReader

DAY = 86_400
CODEX_USAGE = re.compile(rb'"type"\s*:\s*"(session_meta|turn_context|task_started|token_usage_record|token_count)"')
CLAUDE_USAGE = re.compile(rb'"type"\s*:\s*"assistant"')
CODEX_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")


@dataclass(frozen=True, slots=True)
class Call:
    timestamp: float
    model: str | None
    tokens: ModelUsageTotals
    provider: str | None = None


def number(record: dict, key: str) -> int:
    value = record.get(key)
    return value if type(value) is int and value >= 0 else 0


def token_use(input_tokens: int, cache_read: int, cache_write: int, output_tokens: int) -> ModelUsageTotals:
    cache_read = min(cache_read, input_tokens)
    return ModelUsageTotals(
        calls=1,
        cached_calls=int(cache_read > 0),
        input_tokens=input_tokens,
        cached_input_tokens=cache_read,
        cache_write_tokens=min(cache_write, input_tokens - cache_read),
        output_tokens=output_tokens,
    )


def records(lines: Iterable[bytes], marker: re.Pattern[bytes]) -> Iterator[dict[str, Any]]:
    for line in lines:
        if line.endswith(b"\n") and marker.search(line):
            record = json.loads(line)
            if isinstance(record, dict):
                yield record


def call_time(record: dict) -> float:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise ValueError("Usage record has no timestamp")
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        raise ValueError("Usage timestamp has no timezone")
    return parsed.timestamp()


def codex_calls(lines: Iterable[bytes], thread_id: str) -> Iterator[Call]:
    turn = model = None
    provider = None
    models: dict[str, str] = {}
    explicit_turns: set[str | None] = set()
    response_ids: set[str] = set()
    explicit: list[tuple[str | None, Call]] = []
    snapshots: list[tuple[str | None, Call]] = []
    previous = dict.fromkeys(CODEX_TOKEN_FIELDS, 0)
    for record in records(lines, CODEX_USAGE):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = record.get("type")
        if kind == "session_meta":
            if payload.get("id") != thread_id:
                raise ValueError("Codex rollout belongs to a different session")
            observed_provider = payload.get("model_provider")
            provider = observed_provider if isinstance(observed_provider, str) else None
        if kind == "turn_context":
            turn, observed_model = payload.get("turn_id"), payload.get("model")
            if isinstance(observed_model, str):
                model = observed_model
                if isinstance(turn, str):
                    models[turn] = model
        if kind == "event_msg" and payload.get("type") == "task_started":
            turn = payload.get("turn_id")
        if kind == "token_usage_record" and payload.get("thread_id") == thread_id:
            usage, response_id = payload.get("usage"), payload.get("response_id")
            if not isinstance(usage, dict):
                continue
            call_turn = payload.get("turn_id") or turn
            explicit_turns.add(call_turn)
            if isinstance(response_id, str):
                if response_id in response_ids:
                    continue
                response_ids.add(response_id)
            tokens = token_use(*(number(usage, key) for key in CODEX_TOKEN_FIELDS))
            if tokens.input_tokens or tokens.output_tokens:
                explicit.append((call_turn, Call(call_time(record), model, tokens, provider)))
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total, dict):
                continue
            counters = {key: number(total, key) for key in CODEX_TOKEN_FIELDS}
            delta = {key: max(0, counters[key] - previous[key]) for key in CODEX_TOKEN_FIELDS}
            if (
                counters["input_tokens"] + counters["output_tokens"]
                < previous["input_tokens"] + previous["output_tokens"]
            ):
                # Older CLIs could restart the cumulative counter after resuming a session.
                delta = counters
            previous = counters
            tokens = token_use(*(delta[key] for key in CODEX_TOKEN_FIELDS))
            if tokens.input_tokens or tokens.output_tokens:
                snapshots.append((turn, Call(call_time(record), model, tokens, provider)))

    # Modern rollouts retain individual responses (including compaction), alongside
    # UI snapshots. A legacy turn only has cumulative counters.
    for call_turn, call in explicit:
        yield replace(call, model=models.get(call_turn, call.model) if call_turn is not None else call.model)
    for call_turn, call in snapshots:
        if call_turn not in explicit_turns:
            yield call


def claude_calls(lines: Iterable[bytes], thread_id: str) -> Iterable[Call]:
    calls: dict[str, Call] = {}
    for record in records(lines, CLAUDE_USAGE):
        if record.get("type") != "assistant" or record.get("sessionId") != thread_id:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        message_id, usage = message.get("id"), message.get("usage")
        if not isinstance(message_id, str) or not isinstance(usage, dict):
            continue
        read = number(usage, "cache_read_input_tokens")
        write = number(usage, "cache_creation_input_tokens")
        tokens = token_use(number(usage, "input_tokens") + read + write, read, write, number(usage, "output_tokens"))
        if tokens.input_tokens or tokens.output_tokens:
            # Multiple streamed content blocks share a response ID and repeat its usage.
            timestamp = calls[message_id].timestamp if message_id in calls else call_time(record)
            model = message.get("model")
            calls[message_id] = Call(timestamp, model if isinstance(model, str) else None, tokens)
    return calls.values()


def add_usage(totals: ModelUsageTotals, addition: ModelUsageTotals) -> None:
    totals.calls += addition.calls
    totals.cached_calls += addition.cached_calls
    totals.input_tokens += addition.input_tokens
    totals.cached_input_tokens += addition.cached_input_tokens
    totals.cache_write_tokens += addition.cache_write_tokens
    totals.output_tokens += addition.output_tokens
    if addition.cost_usd is not None:
        totals.cost_usd = (totals.cost_usd or 0.0) + addition.cost_usd
        totals.priced_calls += addition.priced_calls


def total_tokens(totals: ModelUsageTotals) -> int:
    return totals.input_tokens + totals.output_tokens


@dataclass(frozen=True, slots=True)
class LogUsage:
    signature: tuple[int, int, int, int]
    all_time: ModelUsageTotals
    latest_at: float
    recent: tuple[Call, ...]


@dataclass(frozen=True, slots=True)
class BoundSession:
    backend: str
    thread_id: str
    context_key: str
    linked: bool
    agent: str | None


@dataclass(slots=True)
class UsageWindow:
    start: float
    total: ModelUsageTotals = field(default_factory=ModelUsageTotals)
    agents: dict[str | None, ModelUsageTotals] = field(default_factory=lambda: defaultdict(ModelUsageTotals))
    models: dict[tuple[str, str | None], ModelUsageTotals] = field(
        default_factory=lambda: defaultdict(ModelUsageTotals)
    )
    sessions: list[tuple[BoundSession, ModelUsageTotals]] = field(default_factory=list)

    def snapshot(self) -> ModelUsageWindow:
        return ModelUsageWindow(
            **self.total.model_dump(),
            by_agent=[
                ModelUsageByAgent(**usage.model_dump(), agent=agent)
                for agent, usage in sorted(self.agents.items(), key=lambda item: -total_tokens(item[1]))
            ],
            by_model=[
                ModelUsageByModel(**usage.model_dump(), backend=backend, model=model)
                for (backend, model), usage in sorted(self.models.items(), key=lambda item: -total_tokens(item[1]))
            ],
            top_sessions=[
                ModelUsageBySession(
                    **usage.model_dump(),
                    backend=session.backend,
                    context_key=redact(session.context_key),
                    thread_id=session.thread_id,
                    session_id=session_key(session.backend, session.thread_id) if session.linked else None,
                )
                for session, usage in nlargest(20, self.sessions, key=lambda item: total_tokens(item[1]))
            ],
        )


def native_logs(root: Path, driver: str, sessions: dict[str, BoundSession]) -> dict[str, tuple[Path, stat_result]]:
    logs: dict[str, tuple[Path, stat_result]] = {}
    if driver == "codex":
        paths = (
            path
            for directory in ("sessions", "archived_sessions")
            for path in (root / directory).rglob("rollout-*.jsonl*")
        )
    else:
        paths = (root / "projects").glob("*/*.jsonl")
    for path in paths:
        if driver == "codex":
            name = path.name.removesuffix(".zst")
            if not name.endswith(".jsonl"):
                continue
            thread_id = name.removesuffix(".jsonl")[-36:]
        else:
            thread_id = path.stem
        if thread_id not in sessions:
            continue
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        previous = logs.get(thread_id)
        # Archiving can briefly expose two paths for the same session.
        if previous is None or (info.st_mtime_ns, path.suffix == ".jsonl") > (
            previous[1].st_mtime_ns,
            previous[0].suffix == ".jsonl",
        ):
            logs[thread_id] = path, info
    return logs


class ModelUsageReader:
    """Project the service's native logs without starting CLI processes or persisting another ledger."""

    def __init__(self, config: NyanpasuConfig, reader: TranscriptReader):
        self.config = config
        self.reader = reader
        self._lock = Lock()
        self._cache: dict[Path, LogUsage] = {}

    async def read(self) -> ModelUsageSnapshot:
        async with self._lock:
            return await to_thread.run_sync(self._read)

    def _sessions(self) -> dict[str, list[BoundSession]]:
        with self.reader.connect() as conn:
            rows = conn.execute("""
                WITH runs AS MATERIALIZED (
                    SELECT task_id,backend,thread_id,context_key,
                           CASE WHEN action='run' AND coalesced_into IS NULL
                                THEN coalesce(json_extract(task_json,'$.kind'),'default') END AS kind
                    FROM task_runs
                )
                SELECT binding.backend,binding.thread_id,min(r.context_key) AS context_key,
                       max(coalesce(r.thread_id=binding.thread_id AND r.backend=binding.backend,0)) AS linked,
                       CASE WHEN min(r.kind)=max(r.kind) THEN min(r.kind) END AS agent
                FROM (
                    SELECT task_id,backend,thread_id FROM runs WHERE thread_id IS NOT NULL
                    UNION ALL
                    SELECT task_id,backend,thread_id FROM task_turns
                ) binding JOIN runs r ON r.task_id=binding.task_id
                GROUP BY binding.backend,binding.thread_id ORDER BY linked DESC,binding.backend
            """).fetchall()
        sessions: dict[str, list[BoundSession]] = defaultdict(list)
        for row in rows:
            sessions[row["backend"]].append(
                BoundSession(row["backend"], row["thread_id"], row["context_key"], bool(row["linked"]), row["agent"])
            )
        return sessions

    def _read_log(
        self, path: Path, info: stat_result, root: Path, driver: str, thread_id: str, cutoff: float
    ) -> LogUsage:
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        cached = self._cache.get(path)
        if cached is not None and cached.signature == signature:
            return cached
        total, latest, recent = ModelUsageTotals(), 0.0, []
        opener = open_rollout(path, root) if driver == "codex" else open_regular_file(root, path.relative_to(root))
        with opener as stream:
            calls = codex_calls(stream, thread_id) if driver == "codex" else claude_calls(stream, thread_id)
            for call in calls:
                if (estimate := estimate_api_cost(call.provider, call.model, call.tokens)) is not None:
                    call = replace(
                        call, tokens=call.tokens.model_copy(update={"cost_usd": estimate, "priced_calls": 1})
                    )
                add_usage(total, call.tokens)
                latest = max(latest, call.timestamp)
                if call.timestamp >= cutoff:
                    recent.append(call)
        return LogUsage(signature, total, latest, tuple(recent))

    def _read(self) -> ModelUsageSnapshot:
        now = time.time()
        day, week, month = windows = tuple(UsageWindow(now - days * DAY) for days in (1, 7, 30))
        unavailable = 0
        sources: dict[tuple[Path, str], dict[str, BoundSession]] = defaultdict(dict)
        for name, sessions in self._sessions().items():
            configured = self.config.backends.get(name)
            if configured is None:
                unavailable += len(sessions)
                continue
            driver = configured.driver
            try:
                # Only resolve configuration used to locate the native session files.
                home_key = "CODEX_HOME" if driver == "codex" else "CLAUDE_CONFIG_DIR"
                env = process_env(
                    configured.process, cwd=self.config.state_dir, backend=driver, only={"HOME", home_key}
                )
                root = (codex_home(env) if driver == "codex" else claude_projects(env).parent).resolve()
            except (OSError, RuntimeError, ValueError):
                unavailable += len(sessions)
                continue
            for session in sessions:
                group = sources[root, driver]
                existing = group.get(session.thread_id)
                if existing is None or (session.linked and not existing.linked):
                    group[session.thread_id] = session

        cache: dict[Path, LogUsage] = {}
        all_time = ModelUsageTotals()
        latest = 0.0
        for (root, driver), sessions in sources.items():
            logs = native_logs(root, driver, sessions)
            unavailable += len(sessions) - len(logs)
            for thread_id, (path, info) in logs.items():
                session = sessions[thread_id]
                try:
                    usage = self._read_log(path, info, root, driver, thread_id, month.start)
                except (OSError, EOFError, ValueError, zstd.ZstdError):
                    unavailable += 1
                    continue
                recent = tuple(call for call in usage.recent if call.timestamp >= month.start)
                cache[path] = usage if recent == usage.recent else replace(usage, recent=recent)
                latest = max(latest, usage.latest_at)
                add_usage(all_time, usage.all_time)
                if not recent:
                    continue
                agent = redact(session.agent)
                session_totals = [(window, ModelUsageTotals()) for window in windows]
                for call in recent:
                    model = (session.backend, redact(call.model))
                    for window, totals in session_totals:
                        if call.timestamp >= window.start:
                            add_usage(totals, call.tokens)
                            add_usage(window.models[model], call.tokens)
                for window, totals in session_totals:
                    if totals.calls:
                        add_usage(window.total, totals)
                        add_usage(window.agents[agent], totals)
                        window.sessions.append((session, totals))
        self._cache = cache
        return ModelUsageSnapshot(
            periods=ModelUsagePeriods(
                last_24h=day.snapshot(), last_7d=week.snapshot(), last_30d=month.snapshot(), all_time=all_time
            ),
            latest_at=latest or None,
            unavailable_sessions=unavailable,
        )
