from __future__ import annotations

import base64
import json
import re
import time
from typing import TYPE_CHECKING, Any

from nyanpasu.models import json_dumps
from nyanpasu.transcript.content import content_page
from nyanpasu.transcript.database import RecordNotFound, now_iso

if TYPE_CHECKING:
    import sqlite3

    from nyanpasu.transcript.database import TranscriptDatabase

BUDGET = 240 * 1024


class CursorError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


SESSION_STATES = """
WITH session_states AS (
    SELECT s.*, coalesce((
        SELECT r.status FROM transcript_tasks t JOIN task_runs r USING(task_id)
        WHERE t.session_id=s.session_id ORDER BY t.started_at DESC LIMIT 1
    ), 'unknown') AS state FROM transcript_sessions s
)
"""


class TranscriptReader:
    def __init__(self, database: TranscriptDatabase):
        self.db = database

    def cursor(self, session: sqlite3.Row, purpose: str, seq: int) -> str:
        return (
            base64.urlsafe_b64encode(
                json_dumps([session["session_id"], session["generation"], purpose, str(seq)]).encode()
            )
            .decode()
            .rstrip("=")
        )

    def decode_cursor(self, session: sqlite3.Row, value: str, purpose: str) -> int:
        try:
            sid, generation, kind, seq = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
            if (
                sid != session["session_id"]
                or kind != purpose
                or not str(seq).isdigit()
                or int(seq) > 9223372036854775807
            ):
                raise ValueError
            if generation != session["generation"]:
                raise CursorError("Transcript generation changed; reload this session", 409)
            return int(seq)
        except CursorError:
            raise
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise CursorError("Invalid cursor for this session and query") from exc

    def window(
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
            raise ValueError("before, after_window, around, and after are mutually exclusive")
        with self.db.connect() as conn:
            session = self.db.session(conn, session_id)
            latest = conn.execute(
                "SELECT coalesce(max(seq),0) FROM transcript_events WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            common = {"session_id": session_id, "generation": session["generation"], "generated_at": now_iso()}
            if after is not None:
                start = self.decode_cursor(session, after, "changes")
                if start > latest:
                    raise CursorError("Cursor is beyond this transcript")
                rows = conn.execute(
                    "SELECT * FROM transcript_changes WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (session_id, start, limit + 1),
                ).fetchall()
                changes, used, cursor = [], 0, start
                for row in rows[:limit]:
                    cost = len(row["data"].encode())
                    if changes and used + cost > BUDGET:
                        break
                    used += cost
                    cursor = row["seq"]
                    changes.append({"seq": str(cursor), "upserts": [json.loads(row["data"])]})
                more = bool(rows and rows[-1]["seq"] > cursor)
                if not more:
                    cursor = latest
                return {
                    **common,
                    "changes": changes,
                    "next_cursor": self.cursor(session, "changes", cursor),
                    "has_more": more,
                }
            where, order = "session_id=?", "DESC"
            args: list[Any] = [session_id]
            if before is not None:
                where += " AND first_seq<?"
                args.append(self.decode_cursor(session, before, "before"))
            elif after_window is not None:
                where += " AND first_seq>?"
                args.append(self.decode_cursor(session, after_window, "forward"))
                order = "ASC"
            elif around is not None:
                target = conn.execute(
                    "SELECT first_seq FROM transcript_entries WHERE session_id=? AND entry_id=?", (session_id, around)
                ).fetchone()
                if target is None:
                    raise RecordNotFound("Entry not found in this session")
                earlier = conn.execute(
                    "SELECT first_seq FROM transcript_entries WHERE session_id=? AND first_seq<=? ORDER BY first_seq DESC LIMIT ?",
                    (session_id, target[0], max(1, limit // 2)),
                ).fetchall()
                where += " AND first_seq>=?"
                args.append(earlier[-1][0])
                order = "ASC"
            rows = conn.execute(
                f"SELECT data,first_seq FROM transcript_entries WHERE {where} ORDER BY first_seq {order} LIMIT ?",
                (*args, limit),
            ).fetchall()
            if around is not None:
                center = next(index for index, row in enumerate(rows) if json.loads(row["data"])["entry_id"] == around)
                rows = sorted(enumerate(rows), key=lambda pair: abs(pair[0] - center))
                rows = [row for _, row in rows]
            entries, used = [], 0
            for row in rows:
                cost = len(row["data"].encode())
                if entries and used + cost > BUDGET:
                    break
                used += cost
                entries.append(json.loads(row["data"]))
            entries.sort(key=lambda entry: int(entry["first_seq"]))
            first = int(entries[0]["first_seq"]) if entries else 0
            last = int(entries[-1]["first_seq"]) if entries else 0
            older = bool(
                conn.execute(
                    "SELECT 1 FROM transcript_entries WHERE session_id=? AND first_seq<? LIMIT 1", (session_id, first)
                ).fetchone()
            )
            newer = bool(
                conn.execute(
                    "SELECT 1 FROM transcript_entries WHERE session_id=? AND first_seq>? LIMIT 1", (session_id, last)
                ).fetchone()
            )
            return {
                **common,
                "entries": entries,
                "before_cursor": self.cursor(session, "before", first) if entries else None,
                "after_window_cursor": self.cursor(session, "forward", last) if entries else None,
                "has_older": older,
                "has_newer": newer,
                "change_cursor": self.cursor(session, "changes", latest),
                "coverage": json.loads(session["coverage"]),
            }

    def sessions(
        self, q: str = "", state: str = "", context: str = "", offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        where, args = ["1=1"], []
        if q:
            where.append("(instr(lower(title),lower(?))>0 OR instr(lower(context_key),lower(?))>0)")
            args.extend([q, q])
        if state:
            where.append("state=?")
            args.append(state)
        if context:
            where.append("context_key=?")
            args.append(context)
        with self.db.connect() as conn:
            total = conn.execute(
                f"{SESSION_STATES} SELECT count(*) FROM session_states WHERE {' AND '.join(where)}", args
            ).fetchone()[0]
            rows = conn.execute(
                f"{SESSION_STATES} SELECT * FROM session_states WHERE {' AND '.join(where)} ORDER BY (state IN ('running','preparing')) DESC, updated_at DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            items = [self._session_dict(conn, row) for row in rows]
        return {"items": items, "total": total, "offset": offset, "has_more": offset + len(items) < total}

    def _session_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["coverage"] = json.loads(data["coverage"])
        lease = conn.execute(
            "SELECT expires_at FROM context_leases WHERE context_key=?", (row["context_key"],)
        ).fetchone()
        latest = conn.execute(
            "SELECT r.status FROM transcript_tasks t JOIN task_runs r USING(task_id) WHERE t.session_id=? ORDER BY t.started_at DESC LIMIT 1",
            (row["session_id"],),
        ).fetchone()
        data["state"] = latest[0] if latest else "unknown"
        data["execution_uncertain"] = data["state"] in {"running", "preparing"} and (
            not lease or lease[0] < time.time()
        )
        data["entry_count"] = conn.execute(
            "SELECT count(*) FROM transcript_entries WHERE session_id=?", (row["session_id"],)
        ).fetchone()[0]
        return data

    def session(self, session_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        with self.db.connect() as conn:
            session = self._session_dict(conn, self.db.session(conn, session_id))
            session["tasks"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT t.*,r.status AS state FROM transcript_tasks t JOIN task_runs r USING(task_id) WHERE session_id=? ORDER BY t.started_at LIMIT ? OFFSET ?",
                    (session_id, limit, offset),
                )
            ]
            session["task_count"] = conn.execute(
                "SELECT count(*) FROM transcript_tasks WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            session["has_more_tasks"] = offset + len(session["tasks"]) < session["task_count"]
            return session

    def entry(self, session_id: str, entry_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            self.db.session(conn, session_id)
            row = conn.execute(
                "SELECT data FROM transcript_entries WHERE session_id=? AND entry_id=?", (session_id, entry_id)
            ).fetchone()
            if row is None:
                raise RecordNotFound("Entry not found in this session")
            return json.loads(row[0])

    def content(
        self, session_id: str, ref: str, offset: int = 0, limit: int = 65536, *, tail: bool = False
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            return content_page(conn, session_id, ref, offset, limit, tail=tail)

    def events(
        self,
        session_id: str,
        *,
        after: str | None = None,
        entry_id: str | None = None,
        around: int | None = None,
        limit: int = 50,
        q: str = "",
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            session = self.db.session(conn, session_id)
            purpose = "events:" + json_dumps([entry_id, q])
            start = self.decode_cursor(session, after, purpose) if after else max(0, (around or 1) - 1)
            where, args = "session_id=? AND seq>?", [session_id, start]
            if entry_id:
                where += " AND entry_id=?"
                args.append(entry_id)
            rows = conn.execute(f"SELECT * FROM transcript_events WHERE {where} ORDER BY seq", args)
            items = []
            position = start
            more = False
            pattern = re.compile(re.escape(q), re.IGNORECASE)
            for row in rows:
                if q and not self._find_content(conn, session_id, row["content_ref"], pattern, len(q)):
                    position = row["seq"]
                    continue
                if len(items) == limit:
                    more = True
                    break
                position = row["seq"]
                record = dict(row)
                record["seq"] = str(record["seq"])
                record["preview"] = content_page(conn, session_id, record["content_ref"], 0, 4096)["text"]
                items.append(record)
            return {
                "items": items,
                "has_more": more,
                "next_cursor": self.cursor(session, purpose, position),
            }

    def search(
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
        pattern = re.compile(re.escape(q), re.IGNORECASE)
        with self.db.connect() as conn:
            session = self.db.session(conn, session_id)
            latest = conn.execute(
                "SELECT coalesce(max(seq),0) FROM transcript_events WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            where, args = ["session_id=?"], [session_id]
            if task_id:
                where.append("task_id=?")
                args.append(task_id)
            if kind:
                where.append("json_extract(data,'$.kind')=?")
                args.append(kind)
            if errors:
                where.append("json_extract(data,'$.state') IN ('failed','declined','interrupted')")
            order = "DESC" if errors else "ASC"
            rows = conn.execute(
                f"SELECT data FROM transcript_entries WHERE {' AND '.join(where)} ORDER BY first_seq {order}", args
            )
            found = []
            for row in rows:
                entry = json.loads(row["data"])
                for block in entry["blocks"]:
                    hit = self._find_content(conn, session_id, block["content_ref"], pattern, len(q))
                    if hit:
                        found.append(
                            {
                                "entry_id": entry["entry_id"],
                                "task_id": entry["task_id"],
                                "turn_id": entry["turn_id"],
                                "title": entry["title"],
                                "observed_at": entry["observed_at"],
                                "block_id": block["block_id"],
                                "content_ref": block["content_ref"],
                                **hit,
                            }
                        )
                    if len(found) > offset + limit:
                        return {
                            "items": found[offset : offset + limit],
                            "has_more": True,
                            "indexed_through": str(latest),
                            "coverage": json.loads(session["coverage"]),
                        }
            return {
                "items": found[offset:],
                "has_more": False,
                "indexed_through": str(latest),
                "coverage": json.loads(session["coverage"]),
            }

    def _find_content(
        self, conn: sqlite3.Connection, session_id: str, ref: str, pattern, overlap: int
    ) -> dict[str, Any] | None:
        offset, carry = 0, ""
        while True:
            page = content_page(conn, session_id, ref, offset, 65536)
            text = carry + page["text"]
            match = pattern.search(text)
            if match:
                return {
                    "offset": offset - len(carry.encode()) + len(text[: match.start()].encode()),
                    "snippet": text[max(0, match.start() - 100) : match.end() + 180],
                }
            if page["next_offset"] is None:
                return None
            carry = text[-overlap:] if overlap else ""
            offset = page["next_offset"]

    def download(self, session_id: str, ref: str):
        with self.db.connect() as conn:
            yield from self._download_in_snapshot(conn, session_id, ref)

    def export(self, session_id: str, format: str, task_id: str | None = None):
        with self.db.connect() as conn:
            session = self.db.session(conn, session_id)
            latest = conn.execute(
                "SELECT coalesce(max(seq),0) FROM transcript_events WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            metadata = {
                "session_id": session_id,
                "task_id": task_id,
                "generated_at": now_iso(),
                "through_seq": str(latest),
                "coverage": json.loads(session["coverage"]),
                "origin": session["origin"],
            }
            if format == "jsonl":
                yield json_dumps({"export": metadata}) + "\n"
                where, args = "session_id=? AND seq<=?", [session_id, latest]
                if task_id:
                    where += " AND task_id=?"
                    args.append(task_id)
                for row in conn.execute(f"SELECT * FROM transcript_events WHERE {where} ORDER BY seq", args):
                    yield '{"observation":' + json_dumps(dict(row)) + ',"event":'
                    yield from self._download_in_snapshot(conn, session_id, row["content_ref"])
                    yield "}\n"
            else:
                yield "# " + session["title"] + "\n\n```json\n" + json.dumps(metadata, indent=2) + "\n```\n\n"
                where, args = "session_id=?", [session_id]
                if task_id:
                    where += " AND task_id=?"
                    args.append(task_id)
                for row in conn.execute(f"SELECT data FROM transcript_entries WHERE {where} ORDER BY first_seq", args):
                    entry = json.loads(row["data"])
                    yield f"## {entry['title']} · {entry['state']}\n\nTask: {entry['task_id']} · Entry: {entry['entry_id']}\n\n"
                    for block in entry["blocks"]:
                        yield f"### {block['block_id']}\n\n"
                        yield from self._download_in_snapshot(conn, session_id, block["content_ref"])
                        yield "\n\n"

    def _download_in_snapshot(self, conn: sqlite3.Connection, session_id: str, ref: str):
        offset = 0
        while True:
            page = content_page(conn, session_id, ref, offset, 65536)
            yield page["text"]
            if page["next_offset"] is None:
                return
            offset = page["next_offset"]
