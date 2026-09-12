from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.transcript import TranscriptStore


def fixture_app():
    from nyanpasu.web import create_app

    config = NyanpasuConfig(state_dir=Path(os.environ["NYANPASU_HOME"]))
    state = StateStore(config.db_path)
    journal = TranscriptStore(config.db_path)
    task = AgentTask(
        task_id="fixture-task",
        context_key="demo:transcript",
        action=TaskAction.RUN,
        prompt="Inspect session transcript rendering",
        metadata={"request": {"title": "Trace a running agent session"}},
    )
    if state.record_task(task):
        session = journal.begin(task, None, "app-server")
        journal.record(
            task.task_id,
            {
                "type": "nyanpasu.input",
                "prompt": "Check the failing test and explain the result.",
                "actual_prompt": "Check the failing test and explain the result.\n\nWorkspace: /tmp/demo\nInstructions: keep changes focused.",
                "cwd": "/tmp/demo",
                "context": {"revision": "abc1234"},
            },
        )
        for index in range(75):
            journal.record(
                task.task_id,
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "fixture-thread",
                        "turnId": "fixture-turn",
                        "item": {
                            "id": f"message-{index}",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": f"### Observation {index + 1}\n\nInspecting the saved evidence for the next step.\n\n- Input and execution context are preserved.\n- Tool results remain associated with their call.",
                        },
                    },
                },
            )
        journal.record(
            task.task_id,
            {
                "method": "item/started",
                "params": {
                    "threadId": "fixture-thread",
                    "turnId": "fixture-turn",
                    "item": {
                        "id": "fixture-tool",
                        "type": "commandExecution",
                        "command": "pytest tests/test_transcript.py -q",
                        "cwd": "/tmp/demo",
                        "status": "inProgress",
                        "aggregatedOutput": "collecting tests…\n",
                    },
                },
            },
        )
        journal.record(
            task.task_id,
            {
                "method": "item/completed",
                "params": {
                    "threadId": "fixture-thread",
                    "turnId": "fixture-turn",
                    "item": {
                        "id": "long-tool",
                        "type": "commandExecution",
                        "command": "cat captured-output.log",
                        "status": "failed",
                        "aggregatedOutput": ("日志🙂 normal output\n" * 10000)
                        + "SEARCH-NEEDLE: an error outside the preview\n"
                        + ("tail output\n" * 10000),
                        "exitCode": 1,
                    },
                },
            },
        )
        journal.record(
            task.task_id,
            {
                "type": "nyanpasu.final",
                "text": "### Full message\n\n" + ("正文🙂 must remain readable.\n\n" * 2500) + "MESSAGE-END",
            },
        )
        state.mark_task_done(
            TaskRunResult(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                thread_id="fixture-thread",
                turn_id="fixture-turn",
                final_message="",
                raw_events=[],
            )
        )
    else:
        with journal.db.connect() as conn:
            session = conn.execute(
                "SELECT session_id FROM transcript_tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()[0]
    app = create_app(config)
    router = APIRouter(prefix="/test")

    @router.post("/append")
    def append():
        journal.record(
            task.task_id,
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": "fixture-thread",
                    "turnId": "fixture-turn",
                    "itemId": "fixture-tool",
                    "delta": "new output from fixture\n",
                },
            },
        )
        return {"session": session}

    app.include_router(router)
    return app


if __name__ == "__main__":
    import tempfile

    import uvicorn

    with tempfile.TemporaryDirectory(prefix="nyanpasu-dashboard-test-") as state_dir:
        os.environ["NYANPASU_HOME"] = state_dir
        uvicorn.run(fixture_app, factory=True, host="127.0.0.1", port=8766)
