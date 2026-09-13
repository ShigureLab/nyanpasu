from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from nyanpasu.config import NyanpasuConfig
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
    items = [
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
    source = MemorySessionSource([turn("fixture-turn", *items)])
    app = create_app(config, session_source=source)
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
