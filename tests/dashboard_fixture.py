from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import SecretStr

from nyanpasu.agent import AgentService
from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.diagnostics import diagnostic
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.transcript.claude import ClaudeHistorySource
from tests.claude_source import SESSION, records, write_session
from tests.session_source import MemorySessionSource, tool, turn


def fixture_app():
    from nyanpasu.web import create_app

    token = os.getenv("NYANPASU_TEST_TOKEN")
    config = NyanpasuConfig(
        state_dir=Path(os.environ["NYANPASU_HOME"]),
        server=ServerConfig(token=SecretStr(token) if token else None),
    )
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
    review = AgentTask(
        task_id="fixture-review",
        context_key="demo:review",
        action=TaskAction.RUN,
        prompt="Review a lifecycle change",
        metadata={"request": {"title": "Review with subtasks"}},
    )
    state.record_task(review)
    state.mark_task_running(review.task_id, None)
    state.bind_task_execution(review.task_id, "fixture-review-thread", "fixture-turn")
    design = state.create_subtask(
        review.task_id, SubtaskRequest(request_key="design", prompt="Reference design", purpose="Reference design")
    )
    audit = state.create_subtask(
        review.task_id, SubtaskRequest(request_key="audit", prompt="Audit tests", purpose="Test audit")
    )
    state.mark_task_running(audit.task_id, None)
    state.bind_task_execution(audit.task_id, "fixture-audit-thread", "fixture-turn")
    experiment = state.create_subtask(
        audit.task_id, SubtaskRequest(request_key="experiment", prompt="Check cleanup", purpose="Cleanup experiment")
    )
    state.wait_for_subtasks(audit.task_id, [experiment.task_id])
    state.mark_task_waiting(audit.task_id)
    evidence = b"Frozen independent design evidence\n"
    digest = hashlib.sha256(evidence).hexdigest()
    artifact = config.state_dir / "artifacts" / "subtasks" / design.task_id / digest
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(evidence)
    state.record_subtask_result(
        design.task_id,
        {
            "summary": "Reference design verified",
            "artifacts": [{"name": "reference.md", "path": str(artifact), "sha256": digest, "bytes": len(evidence)}],
            "data": {},
        },
    )
    state.mark_task_done(
        TaskRunResult(
            task_id=design.task_id,
            status=TaskStatus.COMPLETED,
            thread_id="fixture-design-thread",
            turn_id="fixture-turn",
            final_message="",
        )
    )
    state.wait_for_subtasks(review.task_id, [audit.task_id])
    state.mark_task_waiting(review.task_id)
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
    claude_home = config.state_dir / "claude"
    claude_records = records()
    claude_records.extend(
        [
            {
                "type": "user",
                "uuid": "claude-followup",
                "parentUuid": "claude-final",
                "sessionId": SESSION,
                "timestamp": "2026-09-14T00:01:00Z",
                "message": {"content": "Run the next check"},
            },
            {
                "type": "assistant",
                "uuid": "claude-live-message",
                "parentUuid": "claude-followup",
                "sessionId": SESSION,
                "timestamp": "2026-09-14T00:01:01Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "claude-live-tool",
                            "name": "Bash",
                            "input": {"command": "pytest tests/next.py"},
                        }
                    ]
                },
            },
        ]
    )
    write_session(claude_home, claude_records)
    for task_id, turn_id, title in [
        ("claude-task", "claude-input", "Inspect a Claude Code session"),
        ("claude-followup-task", "claude-followup", "Continue the Claude Code check"),
    ]:
        state.record_task(AgentTask(task_id=task_id, action=TaskAction.RUN, context_key="demo:claude", prompt=title))
        state.mark_task_done(
            TaskRunResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                backend="claude",
                thread_id=SESSION,
                turn_id=turn_id,
                final_message="",
            )
        )
    claude_source = ClaudeHistorySource({"CLAUDE_CONFIG_DIR": str(claude_home)})

    class FixtureAgent(AgentService):
        async def startup(self):
            # Display persisted UI states without recovering them into real model runs.
            return None

    app = create_app(
        config, agent=FixtureAgent(config), session_sources={"codex": source, "claude": claude_source}.__getitem__
    )
    app.state.agent.backends.get("codex").execution.diagnostics.extend(
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

    @router.post("/claude-append")
    def claude_append():
        if not any(record.get("uuid") == "claude-live-output" for record in claude_records):
            claude_records.append(
                {
                    "type": "user",
                    "uuid": "claude-live-output",
                    "parentUuid": "claude-live-message",
                    "sessionId": SESSION,
                    "timestamp": "2026-09-14T00:01:03Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "claude-live-tool",
                                "content": "CLAUDE-LIVE-DONE: 4 passed",
                            }
                        ]
                    },
                }
            )
            write_session(claude_home, claude_records)
        return {"session": "claude:" + SESSION}

    app.include_router(router)
    return app


if __name__ == "__main__":
    import tempfile

    import uvicorn

    with tempfile.TemporaryDirectory(prefix="nyanpasu-dashboard-test-") as state_dir:
        os.environ["NYANPASU_HOME"] = state_dir
        uvicorn.run(fixture_app, factory=True, host="127.0.0.1", port=int(os.getenv("NYANPASU_TEST_PORT", "8766")))
