from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.to_thread as to_thread

from nyanpasu.redaction import redact
from nyanpasu.safe_files import open_regular_file
from nyanpasu.transcript.adapters import item_snapshot
from nyanpasu.transcript.history import HistoryItem, HistoryTurn, SessionHistory, SessionMetadata
from nyanpasu.transcript.instructions import instruction_item
from nyanpasu.transcript.source import iso_time

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import TextIO

    from nyanpasu.codex import CodexSessionSource


@contextmanager
def _open_rollout(path: Path, root: Path) -> Generator[TextIO]:
    with ExitStack() as stack:
        try:
            raw = stack.enter_context(open_regular_file(root, path.relative_to(root)))
        except FileNotFoundError:
            # Codex keeps returning the logical .jsonl path after compressing a cold rollout.
            path = path.with_name(path.name + ".zst")
            raw = stack.enter_context(open_regular_file(root, path.relative_to(root)))
        stream = (
            zstd.open(raw, "rt", encoding="utf-8") if path.suffix == ".zst" else io.TextIOWrapper(raw, encoding="utf-8")
        )
        yield stack.enter_context(stream)


def rollout_annotations(
    thread: dict[str, Any], native_home: Path | None = None, root: Path | None = None
) -> tuple[dict[tuple[str, str], dict[str, str | None]], dict[str | None, list[HistoryItem]]]:
    """Read timing and instructions from Codex's own rollout, without copying its journal."""
    path = thread.get("path")
    if path is None:
        return {}, {}
    if native_home is not None and not Path(path).resolve().is_relative_to(native_home.resolve()):
        raise ValueError("Codex rollout path is outside its native home")
    times = {}
    instructions: dict[str | None, list[HistoryItem]] = {}
    turn_id = None
    try:
        anchor = root or Path(path).parent
        with _open_rollout(Path(path), anchor) as rollout:
            for index, line in enumerate(rollout):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break  # Codex may still be appending the last record.
                    raise
                payload = record.get("payload", {})
                record_type = record.get("type")
                if record_type == "session_meta" and payload.get("id") != thread["id"]:
                    raise ValueError("Codex rollout belongs to a different session")
                if record_type == "turn_context" or (
                    record_type == "event_msg" and payload.get("type") == "task_started"
                ):
                    turn_id = payload.get("turn_id")
                text = ""
                title = ""
                if record_type == "session_meta":
                    base = payload.get("base_instructions")
                    text = base.get("text", "") if isinstance(base, dict) else base or ""
                    title = "System prompt · Codex base instructions"
                elif record_type == "response_item" and payload.get("role") in {"system", "developer"}:
                    text = "\n\n".join(
                        part["text"] for part in payload.get("content", []) if part.get("type") == "input_text"
                    )
                    title = f"{payload['role'].capitalize()} instructions · Codex"
                if text:
                    instructions.setdefault(turn_id, []).append(
                        instruction_item(
                            f"codex-instructions-{record.get('ordinal', index)}",
                            text,
                            title=title,
                            raw=record,
                            recorded_at=record.get("timestamp"),
                        )
                    )
                if record.get("type") != "event_msg" or payload.get("type") not in {"item_started", "item_completed"}:
                    continue
                if payload.get("thread_id") != thread["id"]:
                    continue
                started, completed = payload.get("started_at_ms"), payload.get("completed_at_ms")
                times[(payload["turn_id"], payload["item"]["id"])] = {
                    "started_at": iso_time(started / 1000) if started is not None else None,
                    "completed_at": iso_time(completed / 1000) if completed is not None else None,
                    "recorded_at": record["timestamp"],
                }
    except OSError:
        # Ephemeral sessions and a rollout being archived can have no readable path.
        # The UI marks unavailable times rather than substituting the page read time.
        return {}, {}
    return times, instructions


class CodexHistorySource:
    def __init__(self, client: CodexSessionSource, *, native_home: Path | None = None, root: Path | None = None):
        self.client = client
        self.native_home = native_home
        self.root = root

    @staticmethod
    def _metadata(thread: dict[str, Any]) -> SessionMetadata:
        return SessionMetadata(
            id=thread["id"],
            backend="codex",
            cwd=thread.get("cwd"),
            model=thread.get("model"),
            provider=thread.get("modelProvider"),
            reasoning_effort=thread.get("reasoningEffort"),
            cli_version=thread.get("cliVersion"),
            created_at=iso_time(thread["createdAt"]) if thread.get("createdAt") is not None else None,
            updated_at=iso_time(thread["updatedAt"]) if thread.get("updatedAt") is not None else None,
        )

    async def read_metadata(self, thread_id: str) -> SessionMetadata:
        return self._metadata(await self.client.read_thread(thread_id))

    @staticmethod
    def _item(item: dict, times: dict) -> HistoryItem:
        safe = redact(item)
        return HistoryItem(id=item["id"], presentation=item_snapshot(safe), raw=safe, redacted=safe != item, **times)

    async def read_session(self, thread_id: str) -> SessionHistory:
        thread = await self.client.read_thread(thread_id)
        turns = []
        cursor = None
        while True:
            page = await self.client.list_turns(thread_id, cursor)
            turns.extend(page["data"])
            cursor = page.get("nextCursor")
            if cursor is None:
                break
        times, instructions = await to_thread.run_sync(rollout_annotations, thread, self.native_home, self.root)
        return SessionHistory(
            metadata=self._metadata(thread),
            turns=tuple(
                HistoryTurn(
                    id=turn["id"],
                    items=(tuple(instructions.get(None, ())) if index == 0 else ())
                    + tuple(instructions.get(turn["id"], ()))
                    + tuple(self._item(item, times.get((turn["id"], item["id"]), {})) for item in turn["items"]),
                )
                for index, turn in enumerate(turns)
            ),
        )
