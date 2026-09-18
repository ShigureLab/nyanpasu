from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio.to_thread as to_thread

from nyanpasu.transcript.content import content, redact
from nyanpasu.transcript.history import HistoryItem, HistoryTurn, SessionHistory, SessionMetadata
from nyanpasu.transcript.models import EntryUpdate

if TYPE_CHECKING:
    from collections.abc import Mapping


class ClaudeHistorySource:
    def __init__(self, env: Mapping[str, str]):
        self.projects = (
            Path(env.get("CLAUDE_CONFIG_DIR", str(Path(env.get("HOME", str(Path.home()))) / ".claude"))) / "projects"
        )

    async def read_session(self, thread_id: str) -> SessionHistory:
        return await to_thread.run_sync(self._read, thread_id)

    def _read(self, thread_id: str) -> SessionHistory:
        return claude_history(str(UUID(thread_id)), self._records(thread_id))

    async def read_metadata(self, thread_id: str) -> SessionMetadata:
        return await to_thread.run_sync(self._read_metadata, thread_id)

    def _read_metadata(self, thread_id: str) -> SessionMetadata:
        return claude_metadata(str(UUID(thread_id)), conversation_chain(self._records(thread_id)))

    def _records(self, thread_id: str) -> list[dict[str, Any]]:
        session_id = str(UUID(thread_id))
        paths = list(self.projects.glob(f"*/{session_id}.jsonl"))
        if len(paths) != 1:
            raise RuntimeError("Claude session history is missing or ambiguous")
        records = []
        with paths[0].open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break  # The CLI can be appending the final record.
                    raise
                if not isinstance(record, dict):
                    raise ValueError("Claude transcript record must be an object")
                if record.get("sessionId") == session_id:
                    records.append(record)
        return records


def conversation_chain(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Follow the current branch, including ancestors across compaction boundaries."""
    by_id = {r["uuid"]: r for r in records if r.get("uuid") and not r.get("isSidechain")}
    leaves = [
        r
        for r in records
        if (r.get("type") in {"user", "assistant"} or r.get("subtype") == "compact_boundary") and r.get("uuid") in by_id
    ]
    if not leaves:
        return []
    current = leaves[-1]
    chain = []
    visited: set[str] = set()
    while current:
        key = current["uuid"]
        if key in visited:
            raise ValueError("Cycle in Claude conversation history")
        visited.add(key)
        chain.append(current)
        parent = current.get("parentUuid")
        if current.get("subtype") == "compact_boundary":
            parent = current.get("logicalParentUuid") or parent
        current = by_id.get(parent)
    return list(reversed(chain))


def tool_presentation(block: dict, result: dict | None, cwd: str | None) -> EntryUpdate:
    name, inputs = block["name"], block.get("input", {})
    state = "running" if result is None else "failed" if result.get("is_error") else "completed"
    update = EntryUpdate(
        kind="tool",
        title=name,
        tool_name=name,
        state=state,
        cwd=cwd,
        source_item_id=block["id"],
        blocks=[content("arguments", "json", inputs)],
    )
    if name == "Bash":
        update.command = inputs.get("command")
    elif name in {"Edit", "Write", "MultiEdit"}:
        update.kind = "file_change"
        path = inputs.get("file_path", "file")
        edits = inputs.get("edits", [inputs]) if name != "Write" else []
        diffs = []
        for edit in edits:
            before = edit.get("old_string", "")
            after = edit.get("new_string", edit.get("content", ""))
            diffs.append(
                "".join(
                    difflib.unified_diff(
                        [line + "\n" for line in before.splitlines()],
                        [line + "\n" for line in after.splitlines()],
                        fromfile=path,
                        tofile=path,
                    )
                )
            )
        if any(diffs):
            update.blocks.append(content("diff", "diff", "\n".join(diffs)))
        if name == "Write":
            # The tool input has no previous content; a full-file addition would be misleading.
            update.blocks.append(content("requested content", "text", inputs.get("content", "")))
    elif name in {"Agent", "Task"}:
        update.kind = "collaboration"
    if result is not None:
        value = result.get("content", "")
        update.blocks.append(
            content(
                "output",
                "error" if result.get("is_error") else "combined_output",
                block_text(value) if isinstance(value, (str, list)) else value,
            )
        )
    return update


def block_text(value: str | list) -> str:
    if isinstance(value, str):
        return value
    return "\n\n".join(
        part.get("text", "") if part.get("type") == "text" else json.dumps(part, ensure_ascii=False, indent=2)
        for part in value
    )


def message_blocks(record: dict) -> list[dict]:
    if record.get("type") == "system":
        return (
            [{"type": "compaction", "text": "Conversation compacted", "metadata": record.get("compactMetadata")}]
            if record.get("subtype") == "compact_boundary"
            else []
        )
    if record.get("type") not in {"user", "assistant"}:
        return []
    blocks = record.get("message", {}).get("content", [])
    return [{"type": "text", "text": blocks}] if isinstance(blocks, str) else blocks


def text_presentation(record: dict, block: dict) -> EntryUpdate:
    role, kind = record.get("type"), block.get("type")
    entry_kind = (
        "compaction"
        if kind == "compaction" or record.get("isCompactSummary")
        else "input"
        if role == "user"
        else "reasoning"
        if kind == "thinking"
        else "message"
    )
    return EntryUpdate(
        kind=entry_kind,
        title={
            "input": "User message",
            "message": "Assistant message",
            "reasoning": "Thinking",
            "compaction": "Context compaction",
        }[entry_kind],
        state="failed" if record.get("isApiErrorMessage") or record.get("error") else "recorded",
        source_item_id=record["uuid"],
        phase="final_answer"
        if role == "assistant" and record.get("message", {}).get("stop_reason") == "end_turn"
        else None,
        blocks=[content("text", "markdown", block.get("thinking", block.get("text", "")))],
    )


def history_items(original: dict, results: dict[str, tuple[dict, dict]], cwd: str | None) -> list[HistoryItem]:
    record = redact(original)
    items = []
    for index, block in enumerate(message_blocks(record)):
        kind = block.get("type")
        if kind == "tool_result":
            continue
        item_id = block["id"] if kind == "tool_use" else f"{record['uuid']}:{index}"
        timestamp = record.get("timestamp")
        completed_at = None
        raw = {"message_id": record["uuid"], "block": block}
        redacted = record != original
        if kind == "tool_use":
            pair = results.get(block["id"])
            result = redact(pair[0]) if pair else None
            completed_at = pair[1].get("timestamp") if pair else None
            if pair:
                raw["result"] = result
                redacted = redacted or result != pair[0]
            update = tool_presentation(block, result, cwd)
            native_result = pair[1].get("toolUseResult") if pair else None
            if isinstance(native_result, dict) and native_result.get("interrupted"):
                update.state = "interrupted"
        elif kind in {"text", "thinking", "compaction"}:
            update = text_presentation(record, block)
        elif kind == "redacted_thinking":
            update = EntryUpdate(kind="reasoning", title="Thinking unavailable", blocks=[])
            raw = {"message_id": record["uuid"], "block": {"type": kind}}
        else:
            update = EntryUpdate(
                kind="attachment" if kind in {"image", "document"} else "unknown",
                title=str(kind or "Unknown content"),
                blocks=[content("item", "json", block)],
            )
        items.append(
            HistoryItem(
                id=item_id,
                presentation=update,
                raw=raw,
                redacted=redacted,
                started_at=timestamp if kind == "tool_use" else None,
                completed_at=completed_at,
                recorded_at=timestamp,
            )
        )
    return items


def claude_metadata(session_id: str, chain: list[dict[str, Any]]) -> SessionMetadata:
    metadata = {key: next((r.get(key) for r in reversed(chain) if r.get(key)), None) for key in ("cwd", "version")}
    model = next(
        (
            r.get("message", {}).get("model")
            for r in reversed(chain)
            if r.get("message", {}).get("model") not in (None, "<synthetic>")
        ),
        None,
    )
    timestamps = [r["timestamp"] for r in chain if r.get("timestamp")]
    return SessionMetadata(
        id=session_id,
        backend="claude",
        cwd=metadata["cwd"],
        cli_version=metadata["version"],
        model=model,
        created_at=timestamps[0] if timestamps else None,
        updated_at=timestamps[-1] if timestamps else None,
    )


def claude_history(session_id: str, records: list[dict[str, Any]]) -> SessionHistory:
    chain = conversation_chain(records)
    metadata = claude_metadata(session_id, chain)
    results = {
        block["tool_use_id"]: (block, record)
        for record in chain
        for block in message_blocks(record)
        if block.get("type") == "tool_result"
    }
    turns: dict[str, list[HistoryItem]] = {}
    turn_id = None
    for record in chain:
        blocks = message_blocks(record)
        if not blocks:
            continue
        if (
            record.get("type") == "user"
            and any(b.get("type") != "tool_result" for b in blocks)
            and not record.get("isMeta")
            and not record.get("isCompactSummary")
        ):
            turn_id = record["uuid"]
        if turn_id is None:
            turn_id = record["uuid"]
        turns.setdefault(turn_id, []).extend(history_items(record, results, metadata.cwd))
    return SessionHistory(
        metadata=metadata,
        turns=tuple(HistoryTurn(id=key, items=tuple(items)) for key, items in turns.items() if items),
    )
