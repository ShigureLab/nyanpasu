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

from nyanpasu.memory import MemoryAccess, MemoryConflict, MemoryDenied, MemoryNotFound
from nyanpasu.memory_consolidation import MEMORY_TASK_KINDS
from nyanpasu.memory_context import MemoryContext, build_memory_context
from nyanpasu.memory_search import search_excerpt
from nyanpasu.models import SubtaskRequest
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
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list, max_length=32)
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class TurnControl:
    prompt: str
    file: Path
    memory_context: MemoryContext


class TaskControl:
    """Local, per-turn capabilities. Task identity is assigned by the service."""

    def __init__(self, agent: AgentService):
        self.agent = agent
        self._directory: tempfile.TemporaryDirectory | None = None
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._tokens: dict[str, str] = {}
        self._calls: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def turn(self, task_id: str):
        task = await to_thread.run_sync(self.agent.store.task_request, task_id)
        if task.kind in MEMORY_TASK_KINDS:
            raise MemoryDenied("background memory jobs have no task control capability")
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
            if self.agent.config.memory.enabled and task.memory.read_domains:
                summaries = await to_thread.run_sync(self.agent.memory.list_summaries, task.memory)
            memory_context = build_memory_context(task, summaries, enabled=self.agent.config.memory.enabled)
            prompt = f"""\nNyanpasu task control (for this turn only):
Pipe a JSON request to: {command} -
Alternatively, replace - with a request-file path. Stdin requires no filesystem writes.
Requests:
{{"action":"create","input":{{"request_key":"stable-purpose-key","prompt":"self-contained task","developer_instructions":"role and constraints","revision":"optional pinned commit","purpose":"design","kind":"subtask","execution":{{"backend":"configured backend name","model":"optional model","reasoning":"optional reasoning"}},"memory_enabled":true}}}}
{{"action":"inspect"}}
{{"action":"await","input":{{"task_ids":["child-id"]}}}}
{{"action":"cancel","input":{{"task_ids":["child-id"]}}}}
{{"action":"complete","input":{{"summary":"result","artifacts":["relative/evidence.json"],"data":{{}}}}}}
Create chooses a fresh session and separate workspace. A retry must use the same request_key and input.
An already terminal child is returned with its frozen result; no await is needed to read it.
Use a new request key for a new attempt; a child from an earlier root run cannot be awaited or cancelled by this run.
Only your descendants are inspectable/cancellable. You cannot choose another parent or memory identity.
Execution fields are individually optional; omitted fields use this task kind's configured defaults, not the parent's model.
After await, end this turn; the service resumes you with results. Do not poll or sleep waiting for children.
Before ending a child task, complete freezes its summary and artifact bytes outside its workspace.
Cancel stops execution; retained workspaces and history are reclaimed by context cleanup.
Do not expose the control file or its contents, or include it in evidence. Only the root publishes externally.
"""
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
        store = self.agent.store
        task = await to_thread.run_sync(store.task_request, task_id)
        if task.kind in MEMORY_TASK_KINDS:
            raise MemoryDenied("background memory jobs have no task control capability")
        if action == "create":
            child = await self.agent.create_subtask(task_id, SubtaskRequest.model_validate(payload))
            return {
                "task_id": child.task_id,
                "context_key": child.context_key,
                "spawned_by_task_id": child.spawned_by_task_id,
                "status": await to_thread.run_sync(store.task_status, child.task_id),
                "result": await to_thread.run_sync(store.subtask_result, child.task_id),
                "inputs": child.metadata.get("inputs"),
            }
        if action == "inspect":
            return [
                {
                    "task": item.model_dump(mode="json"),
                    "result": await to_thread.run_sync(store.subtask_result, item.task_id),
                    "inputs": (await to_thread.run_sync(store.task_request, item.task_id)).metadata.get("inputs"),
                }
                for item in await to_thread.run_sync(store.subtasks, task_id)
            ]
        if action in {"await", "cancel"}:
            ids = payload.get("task_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
                raise ValueError("task_ids must be a nonempty list of descendant IDs")
            descendants = {item.task_id for item in await to_thread.run_sync(store.subtasks, task_id)}
            if not set(ids) <= descendants:
                raise ValueError("task_ids must belong to this task's descendants")
            if action == "await":
                return await self.agent.wait_for_subtasks(task_id, ids)
            for identity in ids:
                await self.agent.cancel_subtask(identity)
            return {"cancelled": ids}
        if action == "complete":
            completion = Completion.model_validate(payload)
            return await to_thread.run_sync(self._freeze, task_id, completion)
        if action.startswith("memory."):
            task = await to_thread.run_sync(store.task_request, task_id)
            return await to_thread.run_sync(functools.partial(self._memory, task, action, payload))
        return await self.agent.plugin_control(task_id, action, payload)

    def _memory(self, task, action: str, payload: dict[str, Any]) -> Any:
        service = self.agent.memory
        access = task.memory if self.agent.config.memory.enabled else MemoryAccess()
        if action == "memory.search":
            maximum = self.agent.config.memory.max_results_per_search
            limit = payload.get("limit", maximum)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError("memory search limit must be a positive integer")
            arguments: dict[str, Any] = {**payload, "limit": min(limit, maximum)}
            notes = service.search(access, **arguments)
            return [
                {
                    "id": note.id,
                    "context_key": note.context_key,
                    "title": note.title,
                    "topics": list(note.topics),
                    "body": search_excerpt(note.body, payload["query"]),
                    "revision": note.revision,
                }
                for note in notes
            ]
        if action == "memory.read":
            return service.read(access, **payload).to_dict()
        if action == "memory.describe":
            if payload:
                raise ValueError("memory.describe takes no parameters")
            return service.describe(access)
        if action in {"memory.write", "memory.merge", "memory.delete"}:
            raise MemoryDenied("model memory access is read-only; only the service commits background summaries")
        raise ValueError(f"unknown memory action: {action}")

    def _freeze(self, task_id: str, completion: Completion) -> dict[str, Any]:
        store = self.agent.store
        task = store.task_request(task_id)
        if task.spawned_by_task_id is None:
            raise ValueError("only subtasks produce subtask results")
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
