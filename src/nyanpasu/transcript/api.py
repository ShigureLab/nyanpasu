from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from nyanpasu.transcript.content import redact
from nyanpasu.transcript.models import TranscriptChanges, TranscriptEntry, TranscriptWindow
from nyanpasu.transcript.queries import CursorError

if TYPE_CHECKING:
    from collections.abc import Callable

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.transcript.store import TranscriptStore

PageSize = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
Search = Annotated[str, Query(max_length=256)]


def dashboard_router(
    config: NyanpasuConfig, store: TranscriptStore, runtime_info: Callable[[], dict[str, Any]]
) -> APIRouter:
    router = APIRouter(prefix="/api")
    reader = store.reader

    @router.get("/overview")
    def overview():
        with store.db.connect() as conn:
            counts = dict(conn.execute("SELECT status,count(*) FROM task_runs GROUP BY status").fetchall())
            latest = conn.execute("SELECT max(observed_at) FROM transcript_events").fetchone()[0]
        return {
            "generated_at": time.time(),
            "service": "available",
            "task_counts": counts,
            "last_persisted_at": latest,
            "backend": config.codex.backend,
            "capture_error": runtime_info().get("capture_error"),
        }

    @router.get("/sessions")
    def sessions(q: Search = "", state: str = "", context: str = "", offset: Offset = 0, limit: PageSize = 50):
        return reader.sessions(q, state, context, offset, limit)

    @router.get("/sessions/{session_id}")
    def session(session_id: str, offset: Offset = 0, limit: PageSize = 100):
        return reader.session(session_id, offset, limit)

    @router.get("/sessions/{session_id}/transcript", response_model=TranscriptWindow | TranscriptChanges)
    def transcript(
        session_id: str,
        before: str | None = None,
        after_window: str | None = None,
        around: str | None = None,
        after: str | None = None,
        limit: PageSize = 50,
    ):
        try:
            return reader.window(
                session_id, before=before, after_window=after_window, around=around, after=after, limit=limit
            )
        except CursorError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/sessions/{session_id}/entries/{entry_id}", response_model=TranscriptEntry)
    def entry(session_id: str, entry_id: str):
        return reader.entry(session_id, entry_id)

    @router.get("/sessions/{session_id}/events")
    def events(
        session_id: str,
        after: str | None = None,
        entry: str | None = None,
        around: Annotated[int | None, Query(ge=1)] = None,
        limit: PageSize = 50,
        q: Search = "",
    ):
        try:
            return reader.events(session_id, after=after, entry_id=entry, around=around, limit=limit, q=q)
        except CursorError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("/sessions/{session_id}/search")
    def search(
        session_id: str,
        q: Search = "",
        task: str | None = None,
        kind: str | None = None,
        errors: bool = False,
        offset: Offset = 0,
        limit: PageSize = 50,
    ):
        if not q and not errors:
            raise HTTPException(400, "Provide search text or select errors")
        return reader.search(session_id, q, task_id=task, kind=kind, errors=errors, offset=offset, limit=limit)

    @router.get("/sessions/{session_id}/content/{ref}")
    def content(
        session_id: str,
        ref: str,
        offset: Offset = 0,
        limit: Annotated[int, Query(ge=4, le=65536)] = 65536,
        download: bool = False,
        tail: bool = False,
    ):
        try:
            page = reader.content(session_id, ref, offset, limit, tail=tail)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if download:
            return StreamingResponse(
                reader.download(session_id, ref),
                media_type="text/plain",
                headers={"Content-Disposition": f'attachment; filename="{ref}.txt"'},
            )
        return page

    @router.get("/sessions/{session_id}/export")
    def export(session_id: str, format: Literal["markdown", "jsonl"] = "markdown", task: str | None = None):
        reader.session(session_id, limit=1)
        return StreamingResponse(
            reader.export(session_id, format, task),
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{session_id}.{"md" if format == "markdown" else "jsonl"}"'
            },
        )

    @router.get("/tasks")
    def tasks(q: Search = "", state: str = "", plugin: str = "", offset: Offset = 0, limit: PageSize = 50):
        where, args = ["1=1"], []
        if q:
            where.append("(instr(lower(r.context_key),lower(?))>0 OR instr(lower(r.task_json),lower(?))>0)")
            args.extend([q, q])
        if state:
            where.append("r.status=?")
            args.append(state)
        if plugin:
            where.append("coalesce(json_extract(r.task_json,'$.metadata.plugin_id'),'core')=?")
            args.append(plugin)
        with store.db.connect() as conn:
            total = conn.execute(f"SELECT count(*) FROM task_runs r WHERE {' AND '.join(where)}", args).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT r.task_id,r.context_key,r.action,r.status,r.updated_at,r.created_at,
                       substr(r.error,1,1000) AS error,t.session_id,
                       coalesce(json_extract(r.task_json,'$.metadata.plugin_id'),'core') AS plugin_id,
                       coalesce(json_extract(r.task_json,'$.metadata.request.title'),substr(json_extract(r.task_json,'$.prompt'),1,160),r.task_id) AS title,
                       json_extract(r.result_json,'$.coalesced_into') AS coalesced_into
                FROM task_runs r LEFT JOIN transcript_tasks t USING(task_id)
                WHERE {" AND ".join(where)}
                ORDER BY (r.status='running') DESC,(r.status='queued') DESC,r.updated_at DESC
                LIMIT ? OFFSET ?
            """,
                (*args, limit, offset),
            ).fetchall()
        return {"items": redact([dict(row) for row in rows]), "total": total, "has_more": offset + len(rows) < total}

    @router.get("/tasks/{task_id}")
    def task(task_id: str):
        with store.db.connect() as conn:
            row = conn.execute(
                """
                SELECT r.*,t.session_id FROM task_runs r LEFT JOIN transcript_tasks t USING(task_id) WHERE task_id=?
            """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Task not found")
            data = dict(row)
            task_data = json.loads(data.pop("task_json"))
            result = json.loads(data.pop("result_json") or "{}")
            entry = conn.execute(
                "SELECT entry_id FROM transcript_entries WHERE task_id=? ORDER BY first_seq LIMIT 1", (task_id,)
            ).fetchone()
            data["entry_id"] = entry[0] if entry else None
            data["task"] = task_data
            data["coalesced_into"] = result.get("coalesced_into")
            if data["coalesced_into"]:
                target = conn.execute(
                    "SELECT session_id FROM transcript_tasks WHERE task_id=?", (data["coalesced_into"],)
                ).fetchone()
                data["session_id"] = target[0] if target else None
        return redact(data)

    @router.get("/plugins")
    def plugins():
        with store.db.connect() as conn:
            rows = conn.execute("""
                SELECT coalesce(json_extract(task_json,'$.metadata.plugin_id'),'core') AS plugin_id,
                       status,action,count(*) AS count,max(updated_at) AS last_updated_at
                FROM task_runs GROUP BY plugin_id,status,action
            """).fetchall()
        enabled = set(config.enabled_plugins or config.plugins)
        ids = sorted(enabled | {row["plugin_id"] for row in rows})
        return {
            "items": [
                {
                    "plugin_id": name,
                    "enabled": name in enabled,
                    "tasks": [dict(row) for row in rows if row["plugin_id"] == name],
                    "event_coverage": "Submitted task records; plugin event journal is not exposed",
                }
                for name in ids
            ]
        }

    @router.get("/runtime")
    def runtime():
        with store.db.connect() as conn:
            leases = [dict(row) for row in conn.execute("SELECT * FROM context_leases ORDER BY expires_at DESC")]
        return {
            "backend": config.codex.backend,
            "concurrency": config.runtime.concurrency,
            "leases": leases,
            "generated_at": time.time(),
            **redact(runtime_info()),
        }

    return router
