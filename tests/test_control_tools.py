from __future__ import annotations

import asyncio
import json

import pytest
from jsonschema import ValidationError as SchemaError, validate
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nyanpasu.agent import AgentService
from nyanpasu.control_tools import ToolSpec
from nyanpasu.memory import MemoryAccess
from nyanpasu.models import AgentTask, SubtaskRequest
from nyanpasu.task_control import call_control
from nyanpasu.web import WebPluginRuntime
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin
from tests.test_agent import FakeCodex, FakeWorktrees, _config, _task, fake_backends


@pytest.fixture
async def agent(tmp_path):
    config = _config(tmp_path)
    config = config.model_copy(update={"memory": config.memory.model_copy(update={"enabled": True})})
    service = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, FakeCodex())
    )
    try:
        yield service
    finally:
        await service.shutdown()


def record_root(agent, name, **updates):
    task = agent._admit(_task(name, context_key=name).model_copy(update=updates))
    agent.store.record_task(task)
    agent.store.mark_task_running(task.task_id, None)
    return task


def instructions(prompt):
    return {
        item["action"]: item
        for line in prompt.splitlines()
        if line.startswith('{"action":') and "input_schema" in (item := json.loads(line))
    }


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    message: str = Field(min_length=3, description="Text to echo to the caller.")


@pytest.mark.anyio
async def test_registration_drives_instructions_validation_execution_and_live_visibility(agent):
    calls = []

    async def echo(task: AgentTask, request: EchoInput):
        calls.append((task.task_id, request.message))
        return {"echo": request.message}

    tool = ToolSpec("echo", "Echo a message.", EchoInput, echo, lambda task: bool(task.metadata.get("allow_echo")))
    runtime = WebPluginRuntime(config=agent.config, app=None, agent=agent)
    runtime.add_task_control_tools("example", (tool,))
    task = record_root(agent, "root", metadata={"plugin_id": "example", "allow_echo": True})
    try:
        async with agent.control.turn(task.task_id) as control:
            advertised = instructions(control.prompt)["echo"]
            assert advertised["description"] == "Echo a message."
            assert advertised["input_schema"]["properties"]["message"]["description"] == "Text to echo to the caller."
            valid = {"message": "hello"}
            validate(valid, advertised["input_schema"])
            assert await asyncio.to_thread(call_control, control.file, {"action": "echo", "input": valid}) == {
                "echo": "hello"
            }
            for invalid in ({}, {"message": "x"}, {"message": 123}, {"message": "hello", "parent_id": "other"}):
                with pytest.raises(SchemaError):
                    validate(invalid, advertised["input_schema"])
                with pytest.raises(ValueError):
                    await asyncio.to_thread(call_control, control.file, {"action": "echo", "input": invalid})
            assert calls == [(task.task_id, "hello")]

            # A tool advertised at turn start is checked again using current task state.
            agent.store.update_task_input(task.model_copy(update={"metadata": {"plugin_id": "example"}}))
            with pytest.raises(ValueError, match="not available"):
                await asyncio.to_thread(call_control, control.file, {"action": "echo", "input": valid})
            assert calls == [(task.task_id, "hello")]

        async with agent.control.turn(task.task_id) as control:
            assert "echo" not in instructions(control.prompt)
        other = record_root(agent, "other", metadata={"plugin_id": "another", "allow_echo": True})
        async with agent.control.turn(other.task_id) as control:
            assert "echo" not in instructions(control.prompt)
            with pytest.raises(ValueError, match="unknown task action"):
                await asyncio.to_thread(call_control, control.file, {"action": "echo", "input": valid})
    finally:
        agent.store.mark_task_failed(task.task_id, "test finished")


@pytest.mark.anyio
async def test_root_child_and_memory_visibility_match_call_admission(agent):
    root = record_root(agent, "root", memory=MemoryAccess(("public",)))
    child = agent.store.create_subtask(
        root.task_id,
        SubtaskRequest(request_key="child", prompt="Work independently"),
        execution=agent.config.resolve_execution("subtask"),
    )
    agent.store.mark_task_running(child.task_id, None)
    async with agent.control.turn(root.task_id) as control:
        assert "complete" not in instructions(control.prompt)
        assert {"memory.search", "memory.read", "memory.describe"} <= instructions(control.prompt).keys()
        with pytest.raises(ValueError, match="not available"):
            await asyncio.to_thread(call_control, control.file, {"action": "complete", "input": {"summary": "done"}})
        # No-argument tools also enforce their generated schema.
        with pytest.raises(ValueError, match="Extra inputs"):
            await asyncio.to_thread(
                call_control, control.file, {"action": "inspect", "input": {"task_id": child.task_id}}
            )
    async with agent.control.turn(child.task_id) as control:
        assert "complete" in instructions(control.prompt)

    no_memory = record_root(agent, "no-memory")
    async with agent.control.turn(no_memory.task_id) as control:
        assert not any(name.startswith("memory.") for name in instructions(control.prompt))
        for action in ("memory.search", "memory.read", "memory.describe"):
            with pytest.raises(ValueError, match="not available"):
                await asyncio.to_thread(call_control, control.file, {"action": action})

    # Configuration revocation also blocks a previously advertised tool mid-turn.
    async with agent.control.turn(root.task_id) as control:
        agent.config = agent.config.model_copy(
            update={"memory": agent.config.memory.model_copy(update={"enabled": False})}
        )
        with pytest.raises(ValueError, match="not available"):
            await asyncio.to_thread(call_control, control.file, {"action": "memory.describe"})
    async with agent.control.turn(root.task_id) as control:
        assert not any(name.startswith("memory.") for name in instructions(control.prompt))


@pytest.mark.anyio
async def test_reviewer_tools_are_advertised_and_callable_only_for_owning_roots(agent):
    plugin = GitHubReviewerPlugin()
    agent.add_task_control_tools(plugin.id, plugin.task_control_tools())
    root = record_root(agent, "review", metadata={"plugin_id": plugin.id})
    child = agent.store.create_subtask(
        root.task_id,
        SubtaskRequest(request_key="child", prompt="Review assigned files"),
        execution=agent.config.resolve_execution("subtask"),
    )
    agent.store.mark_task_running(child.task_id, None)
    reviewer_actions = {"review-scope", "review-verify", "ci-refresh"}
    async with agent.control.turn(root.task_id) as control:
        advertised = instructions(control.prompt)
        assert reviewer_actions <= advertised.keys()
        schema = advertised["review-scope"]["input_schema"]
        for valid in ({}, {"inventory_id": "current", "groups": []}):
            validate(valid, schema)
        with pytest.raises(SchemaError):
            validate({"inventory_id": "partial"}, schema)
        with pytest.raises(ValidationError):
            await agent.control.dispatch(root.task_id, "review-scope", {"inventory_id": "partial"})
    for task, error in ((child, "not available"), (record_root(agent, "unrelated"), "unknown task action")):
        async with agent.control.turn(task.task_id) as control:
            assert not reviewer_actions & instructions(control.prompt).keys()
            for action in reviewer_actions:
                with pytest.raises(ValueError, match=error):
                    await asyncio.to_thread(call_control, control.file, {"action": action})


@pytest.mark.anyio
async def test_registration_rejects_ambiguous_dispatch_names(agent):
    async def echo(task: AgentTask, request: EchoInput):
        return request.message

    tool = ToolSpec("echo", "Echo a message.", EchoInput, echo)
    with pytest.raises(ValueError, match="unique"):
        agent.add_task_control_tools("duplicate", (tool, tool))
    with pytest.raises(ValueError, match="shadow"):
        agent.add_task_control_tools("shadow", (ToolSpec("create", "Shadow create.", EchoInput, echo),))
    with pytest.raises(ValueError, match="reserved"):
        agent.add_task_control_tools("memory-writer", (ToolSpec("memory.write", "Write memory.", EchoInput, echo),))
    agent.add_task_control_tools("example", (tool,))
    with pytest.raises(ValueError, match="already registered"):
        agent.add_task_control_tools("example", (tool,))
