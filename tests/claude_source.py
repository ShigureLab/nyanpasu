from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SESSION = "9a924f65-ee81-4b9f-9e68-48ceaab7f52c"


def records(session_id: str = SESSION) -> list[dict]:
    """Representative native JSONL, using UUID and parent fields verified with the CLI."""
    messages = [
        ("user", "claude-input", "Inspect the failing test and update the explanation."),
        (
            "assistant",
            "claude-analysis",
            [
                {"type": "thinking", "thinking": "Check the evidence before editing."},
                {"type": "text", "text": "I will run the focused test."},
            ],
        ),
        (
            "assistant",
            "claude-tool",
            [{"type": "tool_use", "id": "claude-bash", "name": "Bash", "input": {"command": "pytest -q"}}],
        ),
        (
            "user",
            "claude-output",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "claude-bash",
                    "is_error": True,
                    "content": "日志🙂\n" * 3000 + "CLAUDE-NEEDLE assertion failed",
                }
            ],
        ),
        (
            "assistant",
            "claude-edit",
            [
                {
                    "type": "tool_use",
                    "id": "claude-edit-call",
                    "name": "Edit",
                    "input": {
                        "file_path": "/tmp/demo/report.txt",
                        "old_string": "old explanation\n",
                        "new_string": "verified explanation\n",
                    },
                }
            ],
        ),
        (
            "user",
            "claude-edit-output",
            [{"type": "tool_result", "tool_use_id": "claude-edit-call", "content": "The file was updated."}],
        ),
        (
            "assistant",
            "claude-final",
            [
                {
                    "type": "text",
                    "text": "### Claude result\n\nThe test failed; the explanation now matches the evidence.",
                }
            ],
        ),
    ]
    output: list[dict] = []
    parent = None
    for index, (role, uid, blocks) in enumerate(messages):
        output.append(
            {
                "type": role,
                "uuid": uid,
                "parentUuid": parent,
                "sessionId": session_id,
                "timestamp": f"2026-09-14T00:00:{index:02d}Z",
                "cwd": "/tmp/demo",
                "version": "2.1.270",
                "message": {
                    "role": role,
                    "content": blocks,
                    **({"model": "claude-test-model"} if role == "assistant" else {}),
                },
            }
        )
        parent = uid
    output[-1]["message"]["stop_reason"] = "end_turn"
    return output


def write_session(root: Path, data: list[dict] | None = None, session_id: str = SESSION) -> Path:
    path = root / "projects" / "-tmp-demo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (records(session_id) if data is None else data))
    )
    return path
