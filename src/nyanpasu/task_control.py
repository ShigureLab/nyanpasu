from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import secrets
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.to_thread as to_thread
from pydantic import BaseModel, ConfigDict, Field

from nyanpasu.control_tools import EmptyInput, ToolSpec
from nyanpasu.memory import MemoryConflict, MemoryDenied, MemoryNotFound
from nyanpasu.memory_consolidation import MEMORY_TASK_KINDS
from nyanpasu.memory_context import MemoryContext, build_memory_context
from nyanpasu.memory_search import search_excerpt
from nyanpasu.models import AgentTask, SubtaskRequest
from nyanpasu.safe_files import open_regular_file
from nyanpasu.task_control_client import call_control as call_control, command as client_command, main as client_main

if TYPE_CHECKING:
    from nyanpasu.agent import AgentService


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: str
    action: str
    input: dict[str, Any] = Field(default_factory=dict)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    summary: str = Field(min_length=1, description="Supported result for the parent task.")
    artifacts: list[str] = Field(
        default_factory=list, max_length=32, description="Relative evidence paths in this workspace."
    )
    data: dict[str, Any] = Field(default_factory=dict)


class TaskIDs(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    task_ids: list[str] = Field(min_length=1, description="Descendant task IDs owned by this task.")


class MemorySearch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    query: str = Field(max_length=8192, description="Specific terms for the problem or reusable procedure.")
    topics: list[str] = Field(default_factory=list, description="Optional topic filters; never grant access.")
    limit: int | None = Field(
        default=None, gt=0, description="Defaults to and is capped by the configured search maximum."
    )


class MemoryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    source_id: str = Field(description="ID returned by memory.search or a memory: reference.")


@dataclass(frozen=True)
class TurnControl:
    prompt: str
    file: Path
    memory_context: MemoryContext


class TaskControl:
    """Local, per-turn capabilities. Task identity is assigned by the service."""

    def __init__(self, agent: AgentService):
        self.agent = agent
        self._tools: dict[str | None, dict[str, ToolSpec[Any]]] = {
            None: {tool.name: tool for tool in self._builtin_tools()}
        }
        self._directory: tempfile.TemporaryDirectory | None = None
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._tokens: dict[str, str] = {}
        self._calls: dict[str, asyncio.Lock] = {}

    def _builtin_tools(self) -> tuple[ToolSpec[Any], ...]:
        return (
            ToolSpec(
                "create",
                "Start a child in a fresh session and separate workspace. Retry with the same request_key and input; "
                "use a new key for a new attempt. Terminal children include their frozen result. "
                "Omitted execution fields use the child kind's configured defaults, not the parent's model. "
                "Task and memory identities are assigned by the service.",
                SubtaskRequest,
                self._create,
            ),
            ToolSpec(
                "inspect", "Inspect only this task's descendants and their frozen results.", EmptyInput, self._inspect
            ),
            ToolSpec(
                "await",
                "Wait for owned descendants from this root run. After calling, end this turn; "
                "the service resumes you with results. Do not poll or sleep waiting for children.",
                TaskIDs,
                self._await,
            ),
            ToolSpec(
                "cancel",
                "Stop owned descendants from this root run. Context cleanup reclaims retained workspaces and history.",
                TaskIDs,
                self._cancel,
            ),
            ToolSpec(
                "complete",
                "Before ending a child task, freeze its summary and artifact bytes outside its workspace for the parent.",
                Completion,
                self._complete,
                lambda task: task.spawned_by_task_id is not None,
            ),
            ToolSpec(
                "memory.search",
                "Search only authorized memory with BM25 keyword ranking. Use specific terms, then memory.read "
                "for full evidence and provenance. Memory tools are read-only; the service maintains summaries.",
                MemorySearch,
                self._memory_search,
                self._memory_available,
            ),
            ToolSpec(
                "memory.read",
                "Read an authorized memory source. Historical evidence may be outdated; "
                "check applicability and cited sources before using it.",
                MemoryRead,
                self._memory_read,
                self._memory_available,
            ),
            ToolSpec(
                "memory.describe",
                "Describe this task's authorized memory domains, topics and counts.",
                EmptyInput,
                self._memory_describe,
                self._memory_available,
            ),
        )

    def register(self, plugin_id: str, tools: tuple[ToolSpec[Any], ...]) -> None:
        if plugin_id in self._tools:
            raise ValueError(f"task tools already registered for plugin: {plugin_id}")
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)) or set(names) & self._tools[None].keys():
            raise ValueError("plugin task tool names must be unique and cannot shadow built-in actions")
        if any(name.startswith("memory.") for name in names):
            raise ValueError("memory action names are reserved for built-in task tools")
        self._tools[plugin_id] = {tool.name: tool for tool in tools}

    def _task_tools(self, task: AgentTask) -> dict[str, ToolSpec[Any]]:
        if task.kind in MEMORY_TASK_KINDS:
            raise MemoryDenied("background memory jobs have no task control capability")
        plugin_id = task.metadata.get("plugin_id", task.metadata.get("source_plugin_id"))
        return {**self._tools[None], **self._tools.get(plugin_id, {})}

    def available_tools(self, task: AgentTask) -> tuple[ToolSpec[Any], ...]:
        return tuple(tool for tool in self._task_tools(task).values() if tool.available(task))

    def _memory_available(self, task: AgentTask) -> bool:
        return self.agent.config.memory.enabled and bool(task.memory.read_domains)

    @contextlib.asynccontextmanager
    async def turn(self, task_id: str):
        task = await to_thread.run_sync(self.agent.store.task_request, task_id)
        tools = self.available_tools(task)
        async with self._lock:
            if self._server is None:
                self._directory = tempfile.TemporaryDirectory(prefix="nyanpasu-control-")
                self._server = await asyncio.start_unix_server(self._handle, path=self.path, limit=1024 * 1024)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = task_id
        self._calls[token] = asyncio.Lock()
        control = self.path.parent / f"{secrets.token_hex(16)}.json"
        control.write_text(json.dumps({"socket": str(self.path), "token": token}))
        control.chmod(0o600)
        try:
            command = shlex.join(client_command(control))
            summaries = []
            if self._memory_available(task):
                summaries = await to_thread.run_sync(self.agent.memory.list_summaries, task.memory)
            memory_context = build_memory_context(task, summaries, enabled=self.agent.config.memory.enabled)
            prompt = f"""\nNyanpasu task control (for this turn only):
Pipe a JSON request to: {command} -
Alternatively, replace - with a request-file path. Stdin requires no filesystem writes.
Requests:
{{"action":"available action name","input":{{"parameter":"value"}}}}
Omit input for actions with no parameters. Only the actions listed below are available to this task.
Their input schemas supply parameter names, types, defaults and constraints.
Do not expose the control file or its contents, or include it in evidence. Only the root publishes externally.
"""
            prompt += "\n".join(tool.instructions() for tool in tools) + "\n"
            prompt += memory_context.prompt
            yield TurnControl(prompt, control, memory_context)
        finally:
            async with self._calls[token]:
                self._tokens.pop(token, None)
                self._calls.pop(token, None)
                control.unlink(missing_ok=True)

    @property
    def path(self) -> Path:
        assert self._directory is not None
        return Path(self._directory.name) / "control.sock"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = ControlRequest.model_validate(json.loads(await reader.readline()))
            lock = self._calls.get(request.token)
            if lock is None:
                raise ValueError("expired task control")
            async with lock:
                task_id = self._tokens.get(request.token)
                if task_id is None or not await to_thread.run_sync(self.agent.store.task_is_active, task_id):
                    raise ValueError("expired task control")
                result = await self.dispatch(task_id, request.action, request.input)
            response = {"ok": True, "result": result}
        except subprocess.CalledProcessError as exc:
            # Commands may contain authenticated URLs; keep them out of control responses.
            response = {"ok": False, "error": f"Subtask command failed (exit {exc.returncode}); retry the request"}
        except (
            ValueError,
            OSError,
            KeyError,
            RuntimeError,
            TypeError,
            MemoryConflict,
            MemoryDenied,
            MemoryNotFound,
        ) as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def dispatch(self, task_id: str, action: str, payload: dict[str, Any]) -> Any:
        task = await to_thread.run_sync(self.agent.store.task_request, task_id)
        tool = self._task_tools(task).get(action)
        if tool is None:
            raise ValueError(f"unknown task action: {action}")
        return await tool.invoke(task, payload)

    async def _create(self, task: AgentTask, request: SubtaskRequest) -> dict[str, Any]:
        child = await self.agent.create_subtask(task.task_id, request)
        store = self.agent.store
        return {
            "task_id": child.task_id,
            "context_key": child.context_key,
            "spawned_by_task_id": child.spawned_by_task_id,
            "status": await to_thread.run_sync(store.task_status, child.task_id),
            "result": await to_thread.run_sync(store.subtask_result, child.task_id),
            "inputs": child.metadata.get("inputs"),
        }

    async def _inspect(self, task: AgentTask, request: EmptyInput) -> list[dict[str, Any]]:
        store = self.agent.store
        return [
            {
                "task": item.model_dump(mode="json"),
                "result": await to_thread.run_sync(store.subtask_result, item.task_id),
                "inputs": (await to_thread.run_sync(store.task_request, item.task_id)).metadata.get("inputs"),
            }
            for item in await to_thread.run_sync(store.subtasks, task.task_id)
        ]

    async def _check_descendants(self, task: AgentTask, ids: list[str]) -> None:
        descendants = {item.task_id for item in await to_thread.run_sync(self.agent.store.subtasks, task.task_id)}
        if not set(ids) <= descendants:
            raise ValueError("task_ids must belong to this task's descendants")

    async def _await(self, task: AgentTask, request: TaskIDs) -> dict[str, Any]:
        await self._check_descendants(task, request.task_ids)
        return await self.agent.wait_for_subtasks(task.task_id, request.task_ids)

    async def _cancel(self, task: AgentTask, request: TaskIDs) -> dict[str, Any]:
        await self._check_descendants(task, request.task_ids)
        for identity in request.task_ids:
            await self.agent.cancel_subtask(identity)
        return {"cancelled": request.task_ids}

    async def _complete(self, task: AgentTask, request: Completion) -> dict[str, Any]:
        return await to_thread.run_sync(self._freeze, task.task_id, request)

    async def _memory_search(self, task: AgentTask, request: MemorySearch) -> list[dict[str, Any]]:
        maximum = self.agent.config.memory.max_results_per_search
        notes = await to_thread.run_sync(
            functools.partial(
                self.agent.memory.search,
                task.memory,
                request.query,
                topics=request.topics,
                limit=min(request.limit if request.limit is not None else maximum, maximum),
            )
        )
        return [
            {
                "id": note.id,
                "context_key": note.context_key,
                "title": note.title,
                "topics": list(note.topics),
                "body": search_excerpt(note.body, request.query),
                "revision": note.revision,
            }
            for note in notes
        ]

    async def _memory_read(self, task: AgentTask, request: MemoryRead) -> dict[str, Any]:
        source = await to_thread.run_sync(self.agent.memory.read, task.memory, request.source_id)
        return source.to_dict()

    async def _memory_describe(self, task: AgentTask, request: EmptyInput) -> dict[str, Any]:
        return await to_thread.run_sync(self.agent.memory.describe, task.memory)

    def _freeze(self, task_id: str, completion: Completion) -> dict[str, Any]:
        store = self.agent.store
        task = store.task_request(task_id)
        context = store.get_context(task.context_key)
        if context is None or context.session_worktree is None:
            raise ValueError("task has no workspace")
        root = context.session_worktree.resolve()
        destination = self.agent.config.state_dir / "artifacts" / "subtasks" / task_id
        artifacts = []
        contents: dict[Path, bytes] = {}
        for name in completion.artifacts:
            try:
                with open_regular_file(root, (root / name).resolve(strict=True).relative_to(root)) as stream:
                    content = stream.read(10 * 1024 * 1024 + 1)
            except (OSError, ValueError) as exc:
                raise ValueError("artifacts must be regular files inside this task's workspace") from exc
            if len(content) > 10 * 1024 * 1024:
                raise ValueError("artifact exceeds 10 MiB; save a focused excerpt")
            digest = hashlib.sha256(content).hexdigest()
            target = destination / digest
            contents[target] = content
            artifacts.append({"name": name, "path": str(target), "sha256": digest, "bytes": len(content)})
        result = {"summary": completion.summary, "artifacts": artifacts, "data": completion.data}
        if frozen := store.subtask_result(task_id):
            if frozen != result:
                raise ValueError("result is already frozen")
            return frozen
        destination.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            for target, content in contents.items():
                if not target.exists():
                    created.append(target)
                    target.write_bytes(content)
                    target.chmod(0o444)
            store.record_subtask_result(task_id, result)
        except Exception:
            for target in created:
                target.unlink(missing_ok=True)
            raise
        return result

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._directory is not None:
            self._directory.cleanup()
        self._tokens.clear()


if __name__ == "__main__":
    client_main()
