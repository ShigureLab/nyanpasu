from __future__ import annotations

from typing import Any

from nyanpasu.transcript.content import content
from nyanpasu.transcript.models import EntryUpdate

ITEM_KINDS = {
    "userMessage": "input",
    "agentMessage": "message",
    "commandExecution": "tool",
    "mcpToolCall": "tool",
    "dynamicToolCall": "tool",
    "functionCallOutput": "tool",
    "webSearch": "tool",
    "fileChange": "file_change",
    "reasoning": "reasoning",
    "plan": "plan",
    "collabToolCall": "collaboration",
    "collabAgentToolCall": "collaboration",
    "contextCompaction": "compaction",
    "imageView": "attachment",
    "imageGeneration": "attachment",
}


def item_snapshot(item: dict[str, Any]) -> EntryUpdate:
    """Render the item returned by Codex, without inventing client-side messages."""
    source_type = str(item.get("type", "unknown"))
    kind = ITEM_KINDS.get(source_type, "unknown")
    raw_state = item.get("status")
    state = "running" if raw_state == "inProgress" else raw_state or "recorded"
    update = EntryUpdate(
        kind=kind,
        title="User message" if kind == "input" else source_type,
        state=state,
        raw_state=raw_state,
        source_item_id=item["id"],
        phase=item.get("phase"),
        source_truncated=bool(item.get("truncated", False)),
    )
    if kind == "input":
        parts = item.get("content", [])
        text = "\n\n".join(part["text"] for part in parts if part.get("type") == "text")
        if text:
            update.blocks.append(content("text", "markdown", text))
        attachments = [part for part in parts if part.get("type") != "text"]
        if attachments:
            update.blocks.append(content("attachments", "json", attachments))
    elif kind == "tool":
        update.title = str(item.get("tool") or item.get("name") or source_type)
        update.tool_name = update.title
        update.command = item.get("command")
        update.cwd = item.get("cwd")
        update.exit_code = item.get("exitCode")
        update.duration_ms = item.get("durationMs")
        for key in ("arguments", "query", "action"):
            if key in item:
                update.blocks.append(content(key, "json", item[key]))
        for key in ("aggregatedOutput", "output"):
            if item.get(key) is not None:
                update.blocks.append(content("output", "combined_output", item[key]))
        for key in ("result", "error", "contentItems"):
            if item.get(key) is not None:
                update.blocks.append(content(key, "error" if key == "error" else "json", item[key]))
    elif kind in {"message", "reasoning", "plan"}:
        # Only the summary exposed by Codex is displayable; encrypted reasoning
        # content is not a substitute for a missing summary.
        text = item.get("summary", []) if kind == "reasoning" else item.get("text", "")
        if isinstance(text, list) and all(isinstance(part, str) for part in text):
            text = "\n".join(text)
        if text:
            update.blocks.append(content("text", "markdown", text))
    elif kind == "file_change":
        changes = item.get("changes", [])
        diff = "\n\n".join(
            f"--- {change['path']}\n+++ {change['path']}\n{change.get('diff', '')}" for change in changes
        )
        update.blocks.append(content("diff", "diff", diff))
        update.blocks.append(
            content("files", "json", [{"path": change["path"], "operation": change.get("kind")} for change in changes])
        )
    else:
        update.blocks.append(content("item", "json", item))
    if update.command and len(update.command) > 2048:
        update.blocks.append(content("command", "text", update.command))
        update.command = update.command[:2048] + "…"
    return update
