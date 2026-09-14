from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from nyanpasu.store import TASK_RUNS
from nyanpasu.transcript.content import CHUNK_BYTES, content_page, decode, encode, fingerprint, redact
from nyanpasu.transcript.models import Coverage, TranscriptEntry
from nyanpasu.transcript.source import RecordNotFound, Snapshot, iso_time, read_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from nyanpasu.transcript.history import SessionSource

BUDGET = 240 * 1024
TASKS = f"""
    SELECT bound.*, CASE WHEN session_backend='codex' THEN session_thread_id
        ELSE session_backend || ':' || session_thread_id END AS session_id
    FROM (
        SELECT r.*, coalesce(r.thread_id,parent.thread_id) AS session_thread_id,
               r.backend AS session_backend,
               (SELECT expires_at FROM context_leases WHERE context_key=r.context_key) AS lease_expires_at
        FROM ({TASK_RUNS}) r LEFT JOIN task_runs parent ON parent.task_id=r.coalesced_into
    ) bound
"""


class CursorError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def cursor(session_id: str, purpose: str, value: Any) -> str:
    return encode([session_id, purpose, value])


def position(value: str, session_id: str, purpose: str) -> Any:
    try:
        data = decode(value)
    except ValueError as exc:
        raise CursorError(str(exc)) from exc
    if not isinstance(data, list) or len(data) != 3 or data[:2] != [session_id, purpose]:
        raise CursorError("Cursor belongs to a different session or query")
    return data[2]


def title(task: dict[str, Any]) -> str:
    metadata = task.get("metadata", {})
    request = metadata.get("request", {})
    return str(request.get("title") or task.get("prompt") or task["task_id"]).splitlines()[0][:160]


def take(
    entries: list[TranscriptEntry], limit: int, *, reverse: bool = False, budget: int = BUDGET
) -> list[dict[str, Any]]:
    selected, size = [], 0
    for entry in reversed(entries) if reverse else entries:
        value = entry.model_dump(mode="json")
        cost = len(json.dumps(value, ensure_ascii=False).encode())
        if len(selected) >= limit or (selected and size + cost > budget):
            break
        selected.append(value)
        size += cost
    return list(reversed(selected)) if reverse else selected


class TranscriptReader:
    """Task metadata comes from Nyanpasu; every conversation read comes from its native runtime."""

    def __init__(self, db_path: Path, sources: Callable[[str], SessionSource]):
        self.db_path = db_path
        self.sources = sources

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _tasks(self, session_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ({TASKS}) WHERE session_id=? ORDER BY created_at", (session_id,)
            ).fetchall()
        if not rows:
            raise RecordNotFound("Session is not associated with a Nyanpasu task")
        return [dict(row) for row in rows]

    async def snapshot(self, session_id: str) -> Snapshot:
        tasks = self._tasks(session_id)
        task_by_turn = {row["turn_id"]: row["task_id"] for row in tasks if row["turn_id"] and not row["coalesced_into"]}
        return await read_snapshot(
            self.sources(tasks[0]["session_backend"]), tasks[0]["session_thread_id"], task_by_turn
        )

    def sessions(
        self, q: str = "", state: str = "", context: str = "", offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM ({TASKS}) WHERE session_id IS NOT NULL ORDER BY created_at").fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["session_id"], []).append(dict(row))
        items = [self._session(session_id, tasks) for session_id, tasks in groups.items()]
        items = [
            item
            for item in items
            if (not q or q.casefold() in (item["title"] + " " + item["context_key"]).casefold())
            and (not state or item["state"] == state)
            and (not context or context in item["context_key"])
        ]
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "offset": offset,
            "has_more": offset + limit < len(items),
        }

    @staticmethod
    def _session(session_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        latest = tasks[-1]
        return {
            "session_id": session_id,
            "thread_id": latest["session_thread_id"],
            "context_key": latest["context_key"],
            "title": title(json.loads(latest["task_json"])),
            "backend": latest["session_backend"],
            "origin": "native",
            "created_at": iso_time(tasks[0]["created_at"]),
            "updated_at": iso_time(max(task["updated_at"] for task in tasks)),
            "state": latest["status"],
            "execution_uncertain": latest["status"] == "running" and (latest["lease_expires_at"] or 0) < time.time(),
            "task_count": len(tasks),
            "coverage": Coverage().model_dump(),
            "previous_session_id": None,
        }

    async def session(self, session_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        tasks = self._tasks(session_id)
        data = self._session(session_id, tasks)
        data["tasks"] = []
        for row in tasks[offset : offset + limit]:
            task = json.loads(row["task_json"])
            workspace = task.get("workspace") or {}
            data["tasks"].append(
                {
                    "task_id": row["task_id"],
                    "turn_id": row["turn_id"],
                    "title": title(task),
                    "state": row["status"],
                    "cwd": row["event_worktree"],
                    "revision": workspace.get("revision"),
                    "created_at": iso_time(row["created_at"]),
                    "ended_at": iso_time(row["updated_at"]) if row["status"] in {"completed", "failed"} else None,
                }
            )
        data["has_more_tasks"] = offset + limit < len(tasks)
        try:
            history = await self.sources(tasks[0]["session_backend"]).read_session(tasks[0]["session_thread_id"])
            data["runtime"] = history.metadata.model_dump()
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            data["runtime"] = None
            data["history_error"] = str(exc)
        return redact(data)

    @staticmethod
    def _change_position(snapshot: Snapshot) -> dict[str, Any]:
        latest = snapshot.turns[-1] if snapshot.turns else None
        return {
            "turn": latest["id"] if latest else None,
            "revision": fingerprint(
                [latest, [entry.revision_seq for entry in snapshot.entries if latest and entry.turn_id == latest["id"]]]
            ),
            "offset": 0,
        }

    async def window(
        self,
        session_id: str,
        *,
        before: str | None = None,
        after_window: str | None = None,
        around: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if sum(value is not None for value in (before, after_window, around, after)) > 1:
            raise CursorError("Choose one of before, after_window, around, or after")
        snapshot = await self.snapshot(session_id)
        if after is not None:
            return self._changes(snapshot, after, limit)
        entries = list(snapshot.entries)
        if before:
            anchor = position(before, session_id, "window")
            values = take(entries[: snapshot.position(anchor)], limit, reverse=True)
        elif after_window:
            anchor = position(after_window, session_id, "window")
            values = take(entries[snapshot.position(anchor) + 1 :], limit)
        elif around:
            index = snapshot.position(around)
            newer = take(entries[index:], max(1, limit // 2), budget=BUDGET // 2)
            values = take(entries[:index], limit - len(newer), reverse=True, budget=BUDGET // 2)
            values.extend(newer)
        else:
            values = take(entries, limit, reverse=True)
        start = int(values[0]["first_seq"]) - 1 if values else 0
        end = int(values[-1]["first_seq"]) if values else 0
        return {
            "session_id": session_id,
            "generation": snapshot.generation,
            "generated_at": snapshot.read_at,
            "entries": values,
            "before_cursor": cursor(session_id, "window", values[0]["entry_id"]) if start else None,
            "after_window_cursor": cursor(session_id, "window", values[-1]["entry_id"])
            if values and end < len(entries)
            else None,
            "has_older": start > 0,
            "has_newer": end < len(entries),
            "change_cursor": cursor(session_id, "changes", self._change_position(snapshot)),
            "coverage": snapshot.coverage,
        }

    def _changes(self, snapshot: Snapshot, after: str, limit: int) -> dict[str, Any]:
        state = position(after, snapshot.session_id, "changes")
        if not isinstance(state, dict) or set(state) - {"turn", "revision", "offset", "page_revision"}:
            raise CursorError("Invalid change cursor")
        anchor = state.get("turn")
        offset = state.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            raise CursorError("Invalid change offset")
        turn_ids = [turn["id"] for turn in snapshot.turns]
        if anchor is not None and anchor not in turn_ids:
            raise CursorError("Session history changed; reload this session", 409)
        turns = snapshot.turns[turn_ids.index(anchor) :] if anchor is not None else snapshot.turns
        latest = self._change_position(snapshot)
        unchanged = state == latest
        ids = {turn["id"] for turn in turns}
        candidates = [] if unchanged else [entry for entry in snapshot.entries if entry.turn_id in ids]
        revision = fingerprint([turns, [entry.revision_seq for entry in snapshot.entries if entry.turn_id in ids]])
        if state.get("page_revision") != revision:
            offset = 0
        values = take(candidates[offset:], limit)
        end = offset + len(values)
        more = end < len(candidates)
        next_position = {**state, "offset": end, "page_revision": revision} if more else latest
        return {
            "session_id": snapshot.session_id,
            "generation": snapshot.generation,
            "generated_at": snapshot.read_at,
            "changes": [{"seq": revision, "upserts": values}] if values else [],
            "next_cursor": cursor(snapshot.session_id, "changes", next_position),
            "has_more": more,
        }

    async def entry(self, session_id: str, entry_id: str) -> dict[str, Any]:
        return (await self.snapshot(session_id)).entry(entry_id).model_dump(mode="json")

    async def _content(self, session_id: str, ref: str) -> str:
        data = decode(ref)
        if not isinstance(data, list) or len(data) != 4 or data[0] != session_id:
            raise CursorError("Content belongs to a different session")
        snapshot = await self.snapshot(session_id)
        snapshot.entry(data[1])
        if ref not in snapshot.contents:
            raise CursorError("The session item changed; refresh it before reading this content", 409)
        return snapshot.contents[ref]

    async def content(
        self, session_id: str, ref: str, offset: int = 0, limit: int = CHUNK_BYTES, *, tail: bool = False
    ) -> dict[str, Any]:
        return content_page(await self._content(session_id, ref), ref, offset, limit, tail=tail)

    async def download(self, session_id: str, ref: str) -> str:
        return await self._content(session_id, ref)

    async def search(
        self,
        session_id: str,
        q: str,
        *,
        task_id: str | None = None,
        kind: str | None = None,
        errors: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        snapshot = await self.snapshot(session_id)
        pattern = re.compile(re.escape(q), re.IGNORECASE)
        found = []
        for entry in reversed(snapshot.entries) if errors else snapshot.entries:
            if (task_id and entry.task_id != task_id) or (kind and entry.kind != kind):
                continue
            if errors and entry.state not in {"failed", "declined", "interrupted"}:
                continue
            for block in entry.blocks:
                text = snapshot.contents[block.content_ref]
                match = pattern.search(text)
                if match:
                    found.append(
                        {
                            "entry_id": entry.entry_id,
                            "task_id": entry.task_id,
                            "title": entry.title,
                            "block_id": block.block_id,
                            "content_ref": block.content_ref,
                            "offset": len(text[: match.start()].encode()),
                            "snippet": text[max(0, match.start() - 80) : match.end() + 160],
                        }
                    )
                if len(found) > offset + limit:
                    return {"items": found[offset : offset + limit], "has_more": True}
        return {"items": found[offset : offset + limit], "has_more": False}

    async def export(self, session_id: str, format: str, task_id: str | None = None) -> str:
        snapshot = await self.snapshot(session_id)
        if format == "jsonl":
            turns = {entry.turn_id for entry in snapshot.entries if not task_id or entry.task_id == task_id}
            return "".join(
                json.dumps({"turnId": turn["id"], "item": item}, ensure_ascii=False) + "\n"
                for turn in snapshot.turns
                if turn["id"] in turns
                for item in turn["items"]
            )
        output = []
        for entry in snapshot.entries:
            if task_id and entry.task_id != task_id:
                continue
            output.append(f"## {entry.title}\n\nTurn: {entry.turn_id}\n")
            for block in entry.blocks:
                text = snapshot.contents[block.content_ref]
                if block.kind == "markdown":
                    output.append(text)
                else:
                    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
                    output.append(f"{fence}\n{text}\n{fence}")
        return "\n\n".join(output) + "\n"
