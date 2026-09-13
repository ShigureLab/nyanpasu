from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from nyanpasu.config import NyanpasuConfig
from nyanpasu.diagnostics import diagnostic
from nyanpasu.models import AgentTask, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from tests.session_source import MemorySessionSource, tool, turn


def fixture_app():
    from nyanpasu.web import create_app

    config = NyanpasuConfig(state_dir=Path(os.environ["NYANPASU_HOME"]))
    state = StateStore(config.db_path)
    task = AgentTask(
        task_id="fixture-task",
        context_key="demo:transcript",
        action=TaskAction.RUN,
        prompt="Inspect session transcript rendering",
        metadata={"request": {"title": "Trace a running agent session"}},
    )
    state.record_task(task)
    state.mark_task_done(
        TaskRunResult(
            task_id=task.task_id,
            status=TaskStatus.COMPLETED,
            thread_id="fixture-thread",
            turn_id="fixture-turn",
            final_message="",
        )
    )
    items: list[dict[str, Any]] = [
        {
            "id": "input",
            "type": "userMessage",
            "content": [
                {
                    "type": "text",
                    "text": "Check the failing test and explain the result.\n\nWorkspace: /tmp/demo",
                }
            ],
        }
    ]
    for index in range(75):
        items.append(
            {
                "id": f"message-{index}",
                "type": "agentMessage",
                "phase": "commentary",
                "text": f"### Observation {index + 1}\n\nInspecting the saved evidence for the next step.\n\n- Input and execution context are preserved.\n- Tool results remain associated with their call.",
            }
        )
    items[1]["text"] += "\n\n" + ("Earlier content settles after loading.\n\n" * 1800) + "EARLIER-END"
    active_tool = {
        **tool("fixture-tool", "collecting tests…\n", "inProgress"),
        "command": "pytest -q",
        "cwd": "/tmp/demo",
    }
    items.extend(
        [
            active_tool,
            {
                **tool(
                    "long-tool",
                    ("日志🙂 normal output\n" * 10000)
                    + "SEARCH-NEEDLE: an error outside the preview\n"
                    + ("tail output\n" * 10000),
                    "failed",
                ),
                "command": "cat captured-output.log",
                "exitCode": 1,
            },
            {
                "id": "final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "### Full message\n\n" + ("正文🙂 must remain readable.\n\n" * 2500) + "MESSAGE-END",
            },
        ]
    )
    rollout = config.state_dir / "fixture-rollout.jsonl"
    rollout.write_text(
        "".join(
            json.dumps(
                {
                    "timestamp": "2026-09-14T00:00:01.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": "fixture-thread",
                        "turn_id": "fixture-turn",
                        "item": {"id": item["id"]},
                        "started_at_ms": 1789344000000 + index * 2000,
                        "completed_at_ms": None if item["id"] == "fixture-tool" else 1789344001000 + index * 2000,
                    },
                }
            )
            + "\n"
            for index, item in enumerate(items)
        )
    )
    source = MemorySessionSource(
        [turn("fixture-turn", *items)],
        metadata={
            "path": str(rollout),
            "cwd": "/tmp/demo",
            "model": "test-model",
            "modelProvider": "test",
            "createdAt": 1789344000,
            "updatedAt": 1789344200,
        },
    )
    app = create_app(config, session_source=source)
    app.state.agent.codex.diagnostics.extend(
        [
            diagnostic("2026-09-14T00:00:00Z WARN codex_core::network: Reconnecting after a network interruption"),
            diagnostic("2026-09-14T00:00:01Z INFO codex_core::network: Connection restored"),
        ]
    )
    router = APIRouter(prefix="/test")

    @router.post("/append")
    def append():
        active_tool["aggregatedOutput"] += "new output from fixture\n"
        return {"session": "fixture-thread"}

    app.include_router(router)
    return app


if __name__ == "__main__":
    import tempfile

    import uvicorn

    with tempfile.TemporaryDirectory(prefix="nyanpasu-dashboard-test-") as state_dir:
        os.environ["NYANPASU_HOME"] = state_dir
        uvicorn.run(fixture_app, factory=True, host="127.0.0.1", port=8766)
