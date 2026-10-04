from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import SecretStr

from nyanpasu.agent import AgentService
from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.diagnostics import diagnostic
from nyanpasu.memory import MemoryAccess, MemoryService
from nyanpasu.memory_context import build_memory_context
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, TaskRunResult, TaskStatus
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride
from nyanpasu.transcript.claude import ClaudeHistorySource
from tests.claude_source import SESSION, records, write_session
from tests.session_source import MemorySessionSource, tool, turn


def fixture_app():
    from nyanpasu.web import create_app

    token = os.getenv("NYANPASU_TEST_TOKEN")
    config = NyanpasuConfig(
        state_dir=Path(os.environ["NYANPASU_HOME"]),
        server=ServerConfig(token=SecretStr(token) if token else None),
        backends={
            "codex": {"driver": "codex", "defaults": {"model": "configured-review-model", "reasoning": "high"}},
            "claude": {"driver": "claude-code", "defaults": {"model": "configured-small-model", "reasoning": "low"}},
        },
        tasks={
            "kinds": {
                "memory_extraction": {"execution": {"backend": "claude"}},
                "memory_consolidation": {"execution": {"backend": "claude"}},
            }
        },
    )
    state = StateStore(config.db_path)
    task = AgentTask(
        execution=config.resolve_execution(),
        task_id="fixture-task",
        context_key="demo:transcript",
        action=TaskAction.RUN,
        prompt="Inspect session transcript rendering",
        metadata={"request": {"title": "Trace a running agent session"}},
        memory=MemoryAccess(("public", "private:fixture") if token else ("public",)),
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
        execution=config.resolve_execution(),
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
        review.task_id,
        SubtaskRequest(request_key="design", prompt="Reference design", purpose="independent-design"),
        execution=config.resolve_execution(),
    )
    audit = state.create_subtask(
        review.task_id,
        SubtaskRequest(request_key="audit", prompt="Audit tests", purpose="test-audit"),
        execution=config.resolve_execution(),
    )
    state.mark_task_running(audit.task_id, None)
    state.bind_task_execution(audit.task_id, "fixture-audit-thread", "fixture-turn")
    experiment = state.create_subtask(
        audit.task_id,
        SubtaskRequest(request_key="experiment", prompt="Check cleanup", purpose="module-review"),
        execution=config.resolve_execution(),
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
    state.record_task(
        AgentTask(
            task_id="fixture-memory",
            context_key="demo:memory",
            action=TaskAction.RUN,
            prompt="Consolidate shared knowledge",
            kind="memory_consolidation",
            execution=config.resolve_execution("memory_consolidation"),
            memory=MemoryAccess(("public",), "public"),
        )
    )
    state.record_task(
        AgentTask(
            task_id="fixture-extraction",
            context_key="demo:extraction",
            action=TaskAction.RUN,
            prompt="Extract a source summary",
            kind="memory_extraction",
            execution=config.resolve_execution("memory_extraction"),
            memory=MemoryAccess(("public",), "public"),
        )
    )
    state.record_task(
        AgentTask(
            task_id="fixture-runtime",
            context_key="demo:runtime",
            action=TaskAction.RUN,
            prompt="Check active task leases",
            execution=config.resolve_execution(),
        )
    )
    state.mark_task_done(
        TaskRunResult(
            task_id="fixture-runtime",
            status=TaskStatus.COMPLETED,
            thread_id=None,
            turn_id=None,
            final_message="Checked",
        )
    )
    memory = MemoryService(config.memory_dir)
    public_access = MemoryAccess(("public",), "public")
    first = memory.checkpoint_source(
        public_access,
        "fixture-task",
        context_key="demo:transcript",
        input_digest="python-evidence",
        cursor=1,
        complete=True,
        title="Python testing summary",
        body="Use pytest and shared fixtures.",
        topics=("python", "tests"),
        sources=("codex:fixture-thread:fixture-turn:pytest-result",),
    )
    memory.checkpoint_source(
        public_access,
        "fixture-runtime",
        context_key="demo:runtime",
        input_digest="runtime-evidence",
        cursor=1,
        complete=True,
        title="Runtime checks",
        body="Check active task leases.",
        topics=("runtime",),
        sources=("tool:runtime:lease-result",),
    )
    snapshot = memory.snapshot_context(public_access, "demo:transcript", 1)
    memory.publish_summary(
        public_access,
        "demo:transcript",
        1,
        body=f"Start with the [Python source summary](memory:{first.id}) for test evidence.",
        source_ids=list(snapshot.source_revisions),
        source_revisions=snapshot.source_revisions,
        input_digest=snapshot.input_digest,
        expected_revision=None,
    )
    if token:
        for domain, title, body, topic, summary in (
            (
                "private:fixture",
                "Private workspace summary",
                "This source summary belongs to the fixture task's private audience.",
                "private-topic",
                "Private workspace context",
            ),
            (
                "private:other",
                "Other private summary",
                "This source belongs to another private audience.",
                "other-private-topic",
                "Other private context",
            ),
        ):
            private_access = MemoryAccess((domain,), domain)
            context_key = "demo:transcript" if domain == "private:fixture" else "demo:other"
            private = memory.checkpoint_source(
                private_access,
                "fixture-task" if domain == "private:fixture" else "fixture-other",
                context_key=context_key,
                input_digest="private-evidence",
                cursor=1,
                complete=True,
                title=title,
                body=body,
                topics=(topic,),
                sources=("tool:private:confirmed-result",),
            )
            snapshot = memory.snapshot_context(private_access, context_key, 1)
            memory.publish_summary(
                private_access,
                context_key,
                1,
                body=f"{summary}: [read the authorized source](memory:{private.id}).",
                source_ids=[private.id],
                source_revisions=snapshot.source_revisions,
                input_digest=snapshot.input_digest,
                expected_revision=None,
            )
    state.bind_task_execution(
        task.task_id,
        "fixture-thread",
        "fixture-turn",
        memory_context=asdict(build_memory_context(task, memory.list_summaries(task.memory))),
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
        state.record_task(
            AgentTask(
                execution=config.resolve_execution(override=ExecutionOverride(backend="claude")),
                task_id=task_id,
                action=TaskAction.RUN,
                context_key="demo:claude",
                prompt=title,
            )
        )
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
