from __future__ import annotations

import json
from typing import Any

from nyanpasu.transcript.models import ContentUpdate, EntryUpdate

# Public app-server v2 schema, checked against codex-cli 0.153.4.
ITEM_KINDS = {
    "agentMessage": "message",
    "agent_message": "message",
    "userMessage": "input",
    "commandExecution": "tool",
    "command_execution": "tool",
    "mcpToolCall": "tool",
    "mcp_tool_call": "tool",
    "dynamicToolCall": "tool",
    "webSearch": "tool",
    "web_search": "tool",
    "fileChange": "file_change",
    "file_change": "file_change",
    "reasoning": "reasoning",
    "plan": "plan",
    "todo_list": "plan",
    "collabAgentToolCall": "collaboration",
    "collab_tool_call": "collaboration",
    "contextCompaction": "compaction",
    "imageView": "attachment",
    "imageGeneration": "attachment",
}
LIFECYCLE_METHODS = {
    "turn/completed",
    "turn.completed",
    "turn.failed",
    "thread.started",
    "thread/started",
    "turn/started",
    "turn.started",
    "error",
}
DELTA_KINDS = {
    "item/agentMessage/delta": ("message", "text", "markdown"),
    "item/reasoning/summaryTextDelta": ("reasoning", "text", "markdown"),
    "item/commandExecution/outputDelta": ("tool", "output", "combined_output"),
}


def display(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def content(block_id: str, kind: str, value: Any, *, append: bool = False) -> ContentUpdate:
    return ContentUpdate(block_id=block_id, kind=kind, text=display(value), append=append)


def normalize(event: dict[str, Any], direction: str) -> EntryUpdate:
    method = str(event.get("method") or event.get("type") or "unknown")
    raw_params = event.get("params")
    params = raw_params if isinstance(raw_params, dict) else event
    if method.startswith("nyanpasu."):
        return lifecycle(event, method)
    if direction in {"server_request", "client_response"}:
        return approval(event, direction, method)
    if isinstance(params.get("item"), dict):
        return item_snapshot(params["item"], method)
    item_id = params.get("itemId") or params.get("item_id")
    if item_id and method in DELTA_KINDS:
        kind, block_id, block_kind = DELTA_KINDS[method]
        # Deltas deliberately omit title/state: a late delta must not reopen a
        # completed tool or replace its name with a generic label.
        return EntryUpdate(
            key=f"item:{item_id}",
            source_item_id=str(item_id),
            kind=kind,
            blocks=[content(block_id, block_kind, params.get("delta", ""), append=True)],
        )
    if method == "turn/plan/updated":
        return EntryUpdate(kind="plan", title="Plan updated", blocks=[content("plan", "json", params.get("plan", []))])
    if method in LIFECYCLE_METHODS:
        turn = params.get("turn", {})
        state = turn.get("status") if isinstance(turn, dict) else None
        return EntryUpdate(
            kind="runtime",
            title=method,
            state=state or ("failed" if method in {"turn.failed", "error"} else "recorded"),
            blocks=[content("record", "json", event)],
        )
    return EntryUpdate(kind="unknown", title=method, state="recorded", blocks=[content("record", "json", event)])


def lifecycle(event: dict[str, Any], method: str) -> EntryUpdate:
    if method == "nyanpasu.input":
        blocks = [content("prompt", "markdown", event["prompt"])]
        missing = []
        if "actual_prompt" in event:
            blocks.append(content("actual", "actual_input", event["actual_prompt"]))
        else:
            missing.append("Actual submitted input was not recorded")
        blocks.append(content("context", "json", event.get("context", {})))
        return EntryUpdate(
            key="input", kind="input", title="Task input", cwd=event.get("cwd"), blocks=blocks, missing_parts=missing
        )
    if method == "nyanpasu.final":
        return EntryUpdate(
            key="fallback",
            kind="message",
            title="Final response (result fallback)",
            phase="final_answer",
            state="completed",
            blocks=[content("text", "markdown", event["text"])],
        )
    if method == "nyanpasu.stderr":
        return EntryUpdate(
            key="stderr",
            kind="runtime",
            title="Backend stderr",
            blocks=[content("stderr", "stderr", event.get("text", ""), append=True)],
        )
    return EntryUpdate(
        kind="runtime",
        title=event.get("title", method.removeprefix("nyanpasu.")),
        state=event.get("state", "recorded"),
        blocks=[content("text", "text", event["text"])] if "text" in event else [content("record", "json", event)],
        missing_parts=[str(event.get("text", "Capture gap"))] if method == "nyanpasu.capture_gap" else [],
    )


def approval(event: dict[str, Any], direction: str, method: str) -> EntryUpdate:
    is_request = direction == "server_request"
    response = event.get("result", {})
    decision = None
    if not is_request:
        decision = str(response.get("decision") or ("empty answers" if "answers" in response else "returned"))
    return EntryUpdate(
        key=f"rpc:{event.get('id')}",
        kind="approval",
        title=event.get("request_method", method),
        state="pending" if is_request else "responded",
        decision=decision,
        blocks=[content("request" if is_request else "response", "json", event)],
    )


def item_snapshot(item: dict[str, Any], method: str) -> EntryUpdate:
    source_type = str(item.get("type", "unknown"))
    kind = ITEM_KINDS.get(source_type, "unknown")
    raw_state = item.get("status")
    state = {"inProgress": "running", "in_progress": "running"}.get(str(raw_state), raw_state)
    if state is None:
        state = "completed" if method in {"item/completed", "item.completed"} else "running"
    update = EntryUpdate(
        key=f"item:{item['id']}" if item.get("id") else None,
        kind=kind,
        title=source_type,
        state=state,
        raw_state=raw_state,
        source_item_id=item.get("id"),
        phase=item.get("phase"),
        source_truncated=bool(item.get("truncated", False)),
    )
    if kind == "tool":
        return tool_snapshot(item, update)
    if kind in {"message", "reasoning", "plan"}:
        # Only summaries explicitly exposed by the backend, never encrypted or
        # hidden reasoning. Unknown content remains in the original event.
        text = item.get("summary", item.get("text", "")) if kind == "reasoning" else item.get("text")
        if text is not None:
            if isinstance(text, list) and all(isinstance(part, str) for part in text):
                text = "\n".join(text)
            update.blocks.append(content("text", "markdown", text))
        if kind == "plan" and "items" in item:
            update.blocks.append(content("plan", "json", item["items"]))
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
    return update


def tool_snapshot(item: dict[str, Any], update: EntryUpdate) -> EntryUpdate:
    name = str(item.get("tool") or item.get("name") or item.get("type"))
    update.title = name
    update.tool_name = name
    update.command = item.get("command")
    update.cwd = item.get("cwd")
    update.exit_code = item.get("exitCode", item.get("exit_code"))
    update.duration_ms = item.get("durationMs")
    for key in ("arguments", "query"):
        if key in item:
            update.blocks.append(content(key, "json", item[key]))
    # Aliases name the same output snapshot; distinct result blocks retain their identity.
    for key in ("aggregatedOutput", "aggregated_output", "output"):
        if item.get(key) is not None:
            update.blocks.append(content("output", "combined_output", item[key]))
            break
    for key in ("result", "error", "contentItems"):
        if item.get(key) is not None:
            update.blocks.append(content(key, "error" if key == "error" else "json", item[key]))
    return update


def identities(event: dict[str, Any]) -> tuple[str | None, str | None]:
    raw_params = event.get("params")
    params = raw_params if isinstance(raw_params, dict) else event
    thread = params.get("threadId") or params.get("thread_id")
    turn = params.get("turnId") or params.get("turn_id")
    for data in (params, event.get("result", {})):
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("thread"), dict):
            thread = data["thread"].get("id", thread)
        if isinstance(data.get("turn"), dict):
            turn = data["turn"].get("id", turn)
    return str(thread) if thread else None, str(turn) if turn else None
