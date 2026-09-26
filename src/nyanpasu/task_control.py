from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.to_thread as to_thread
from pydantic import BaseModel, ConfigDict, Field

from nyanpasu.models import SubtaskRequest

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
            command = shlex.join([sys.executable, "-m", "nyanpasu.task_control", str(control)])
            yield f"""\nNyanpasu subtask control (for this turn only):
Pipe a JSON request to: {command} -
Alternatively, replace - with a request-file path. Stdin requires no filesystem writes.
Requests:
{{"action":"create","input":{{"request_key":"stable-purpose-key","prompt":"self-contained task","developer_instructions":"role and constraints","revision":"optional pinned commit","purpose":"design"}}}}
{{"action":"inspect"}}
{{"action":"await","input":{{"task_ids":["child-id"]}}}}
{{"action":"cancel","input":{{"task_ids":["child-id"]}}}}
{{"action":"complete","input":{{"summary":"result","artifacts":["relative/evidence.json"],"data":{{}}}}}}
Create chooses a fresh session and separate workspace. A retry must use the same request_key and input.
An already terminal child is returned with its frozen result; no await is needed to read it.
Use a new request key for a new attempt; a child from an earlier root run cannot be awaited or cancelled by this run.
Only your descendants are inspectable/cancellable. You cannot choose another parent or backend.
After await, end this turn; the service resumes you with results. Do not poll or sleep waiting for children.
Before ending a child task, complete freezes its summary and artifact bytes outside its workspace.
Cancel stops execution; retained workspaces and history are reclaimed by context cleanup.
Do not expose the control file or its contents, or include it in evidence. Only the root publishes externally.
"""
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
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def dispatch(self, task_id: str, action: str, payload: dict[str, Any]) -> Any:
        store = self.agent.store
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
        raise ValueError(f"unknown subtask action: {action}")

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
            source = (root / name).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                raise ValueError("artifacts must be files inside this task's workspace")
            if source.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("artifact exceeds 10 MiB; save a focused excerpt")
            content = source.read_bytes()
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


def call_control(control: Path, request: dict[str, Any]) -> dict[str, Any]:
    capability = json.loads(control.read_text())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(capability["socket"])
        client.sendall(json.dumps({**request, "token": capability["token"]}).encode() + b"\n")
        with client.makefile("rb") as reader:
            response = json.loads(reader.readline())
    if not response["ok"]:
        raise ValueError(response["error"])
    return response["result"]


if __name__ == "__main__":
    try:
        raw = sys.stdin.read() if sys.argv[2] == "-" else Path(sys.argv[2]).read_text()
        print(json.dumps(call_control(Path(sys.argv[1]), json.loads(raw)), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
