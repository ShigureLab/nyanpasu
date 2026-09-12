from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from nyanpasu.models import AgentTask, json_dumps
from nyanpasu.transcript.adapters import identities, normalize
from nyanpasu.transcript.content import put_content, redact
from nyanpasu.transcript.database import TranscriptDatabase, now_iso
from nyanpasu.transcript.models import ContentUpdate, Coverage, EntryUpdate, Source
from nyanpasu.transcript.projection import merge_coverage, project_entry
from nyanpasu.transcript.queries import TranscriptReader

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


class TranscriptStore:
    def __init__(self, db_path: Path):
        self.db = TranscriptDatabase(db_path)
        self.reader = TranscriptReader(self.db)

    def begin(self, task: AgentTask, thread_id: str | None, backend: str, *, legacy: bool = False) -> str:
        with self.db.connect(write=True) as conn:
            bound = conn.execute("SELECT session_id FROM transcript_tasks WHERE task_id=?", (task.task_id,)).fetchone()
            if bound:
                return bound["session_id"]
            previous = conn.execute(
                "SELECT * FROM transcript_sessions WHERE context_key=? ORDER BY created_at DESC LIMIT 1",
                (task.context_key,),
            ).fetchone()
            reusable = (
                not legacy
                and thread_id
                and previous
                and not previous["closed"]
                and previous["thread_id"] == thread_id
                and previous["origin"] != "legacy-result"
            )
            session_id = (
                previous["session_id"]
                if reusable
                else self._new_session(conn, task, thread_id, backend, legacy, previous)
            )
            conn.execute(
                """INSERT INTO transcript_tasks(task_id,session_id,revision,started_at,title)
                   VALUES (?,?,?,?,?)""",
                (
                    task.task_id,
                    session_id,
                    task.workspace.revision if task.workspace else None,
                    now_iso(),
                    self.task_title(task),
                ),
            )
            return session_id

    def _new_session(
        self,
        conn: sqlite3.Connection,
        task: AgentTask,
        thread_id: str | None,
        backend: str,
        legacy: bool,
        previous: sqlite3.Row | None,
    ) -> str:
        session_id = "s_" + uuid.uuid4().hex
        timestamp = now_iso()
        coverage = Coverage(
            capture_gap=legacy,
            missing_parts=["Historical capture may be incomplete"] if legacy else [],
        )
        if previous and not legacy:
            conn.execute("UPDATE transcript_sessions SET closed=1 WHERE session_id=?", (previous["session_id"],))
        conn.execute(
            """INSERT INTO transcript_sessions(
                session_id,context_key,thread_id,backend,title,generation,created_at,
                updated_at,closed,previous_session_id,origin,coverage
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                task.context_key,
                thread_id,
                backend,
                self.task_title(task),
                uuid.uuid4().hex,
                timestamp,
                timestamp,
                int(legacy),
                previous["session_id"] if previous else None,
                "legacy-result" if legacy else "native",
                coverage.model_dump_json(),
            ),
        )
        return session_id

    @staticmethod
    def task_title(task: AgentTask) -> str:
        request = task.metadata.get("request", {})
        title = request.get("title") if isinstance(request, dict) else None
        return str(redact(title or task.prompt.split("\n")[0] or task.task_id))[:160]

    def record(
        self,
        task_id: str,
        event: dict[str, Any],
        direction: str = "notification",
        version: str | None = None,
        origin: str | None = None,
        source_id: str | None = None,
    ) -> str | None:
        cleaned = redact(event)
        try:
            update = normalize(cleaned, direction)
        except (ValidationError, KeyError, TypeError) as exc:
            update = EntryUpdate(
                kind="unknown",
                title="Invalid backend event",
                state="recorded",
                blocks=[ContentUpdate(block_id="error", kind="error", text=str(exc))],
                missing_parts=["Event normalization failed; original observation retained in Events"],
            )
        with self.db.connect(write=True) as conn:
            session = self._event_session(conn, task_id, cleaned)
            session_id = session["session_id"]
            if (
                source_id
                and conn.execute(
                    "SELECT 1 FROM transcript_events WHERE session_id=? AND task_id=? AND source_id=?",
                    (session_id, task_id, source_id),
                ).fetchone()
            ):
                return None
            timestamp = now_iso()
            raw_ref = put_content(conn, session_id, json_dumps(cleaned))
            cursor = conn.execute(
                """INSERT INTO transcript_events(session_id,task_id,direction,type,observed_at,content_ref,source_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    session_id,
                    task_id,
                    direction,
                    cleaned.get("method", cleaned.get("type", "unknown")),
                    timestamp,
                    raw_ref,
                    source_id,
                ),
            )
            seq = cursor.lastrowid
            assert seq is not None
            thread_id, turn_id = self._bind_event(conn, session, task_id, cleaned)
            entry = project_entry(
                conn,
                session=session,
                task_id=task_id,
                update=update,
                seq=seq,
                timestamp=timestamp,
                thread_id=thread_id,
                turn_id=turn_id,
                source=Source(backend=session["backend"], version=version, origin=origin or session["origin"]),
                redacted=cleaned != event,
            )
            merge_coverage(conn, session, update, redacted=cleaned != event)
            conn.execute("UPDATE transcript_sessions SET updated_at=? WHERE session_id=?", (timestamp, session_id))
            if cleaned.get("type") == "nyanpasu.input":
                conn.execute("UPDATE transcript_tasks SET cwd=? WHERE task_id=?", (cleaned.get("cwd"), task_id))
            if cleaned.get("type") in {"nyanpasu.completed", "nyanpasu.failed", "nyanpasu.interrupted"}:
                conn.execute("UPDATE transcript_tasks SET ended_at=? WHERE task_id=?", (timestamp, task_id))
            return entry.entry_id

    def _event_session(self, conn: sqlite3.Connection, task_id: str, event: dict[str, Any]) -> sqlite3.Row:
        session = conn.execute(
            "SELECT s.* FROM transcript_sessions s JOIN transcript_tasks t USING(session_id) WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        thread_id, _ = identities(event)
        if thread_id and session["thread_id"] and thread_id != session["thread_id"]:
            raw_task = conn.execute("SELECT task_json FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
            task = AgentTask.model_validate(json.loads(raw_task))
            new_id = self._new_session(conn, task, thread_id, session["backend"], False, session)
            conn.execute("UPDATE transcript_tasks SET session_id=? WHERE task_id=?", (new_id, task_id))
            return self.db.session(conn, new_id)
        return session

    def _bind_event(
        self, conn: sqlite3.Connection, session: sqlite3.Row, task_id: str, event: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        task = conn.execute("SELECT turn_id FROM transcript_tasks WHERE task_id=?", (task_id,)).fetchone()
        thread_id, turn_id = identities(event)
        thread_id = thread_id or session["thread_id"]
        turn_id = turn_id or task["turn_id"]
        if thread_id != session["thread_id"]:
            conn.execute(
                "UPDATE transcript_sessions SET thread_id=? WHERE session_id=?", (thread_id, session["session_id"])
            )
        if turn_id != task["turn_id"]:
            conn.execute("UPDATE transcript_tasks SET turn_id=? WHERE task_id=?", (turn_id, task_id))
        return thread_id, turn_id

    def finish_message(self, task_id: str, text: str) -> None:
        with self.db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM transcript_entries WHERE task_id=? AND json_extract(data,'$.phase')='final_answer'",
                (task_id,),
            ).fetchone()
        if text and not exists:
            self.record(task_id, {"type": "nyanpasu.final", "text": text}, origin="result-fallback")

    def close_context(self, context_key: str) -> None:
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE transcript_sessions SET closed=1 WHERE context_key=?", (context_key,))

    def import_legacy(self) -> int:
        """Import terminal pre-journal tasks once. Do not guess thread continuity."""
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT r.* FROM task_runs r
                WHERE r.status IN ('completed','failed') AND r.action='run'
                  AND NOT EXISTS (SELECT 1 FROM transcript_tasks t JOIN transcript_sessions s USING(session_id) WHERE t.task_id=r.task_id AND s.origin<>'legacy-result')
                  AND NOT EXISTS (SELECT 1 FROM transcript_imports i WHERE i.task_id=r.task_id)
                ORDER BY r.created_at
            """).fetchall()
        imported = 0
        for row in rows:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            if result.get("coalesced_into"):
                continue
            task = AgentTask.model_validate(json.loads(row["task_json"]))
            self.begin(task, row["thread_id"], "legacy", legacy=True)
            self.record(task.task_id, {"type": "nyanpasu.input", "prompt": task.prompt}, source_id="legacy-input")
            for index, event in enumerate(result.get("raw_events", [])):
                self.record(task.task_id, event, source_id=f"legacy-{index}")
            self.finish_message(task.task_id, result.get("final_message", ""))
            self.record(
                task.task_id,
                {
                    "type": "nyanpasu." + row["status"],
                    "state": row["status"],
                    "text": row["error"]
                    or "Imported terminal task result; event timestamps are import observation times.",
                },
                source_id="legacy-terminal",
            )
            with self.db.connect(write=True) as conn:
                conn.execute("INSERT OR IGNORE INTO transcript_imports VALUES (?)", (task.task_id,))
            imported += 1
        return imported
