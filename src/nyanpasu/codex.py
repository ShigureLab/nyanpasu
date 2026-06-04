from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio

from nyanpasu.models import CodexRunResult

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig

SUBPROCESS_BUFFER_LIMIT = 64 * 1024 * 1024


class CodexBackend(Protocol):
    async def run_turn(self, *, cwd: Path, prompt: str, thread_id: str | None) -> CodexRunResult: ...

    async def cleanup_thread(self, thread_id: str) -> None: ...

    async def close(self) -> None: ...


def backend_from_config(config: NyanpasuConfig) -> CodexBackend:
    if config.codex.backend == "exec":
        return CodexExecBackend(config)
    if config.codex.backend == "app-server":
        return CodexAppServerBackend(config)
    raise ValueError(f"unknown codex backend: {config.codex.backend}")


class CodexExecBackend:
    def __init__(self, config: NyanpasuConfig) -> None:
        self.config = config

    async def run_turn(self, *, cwd: Path, prompt: str, thread_id: str | None) -> CodexRunResult:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as output_file:
            output_path = Path(output_file.name)
        argv = self._argv(cwd=cwd, thread_id=thread_id, output_path=output_path)
        raw_events: list[dict[str, Any]] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=safe_codex_env(self.config),
                limit=SUBPROCESS_BUFFER_LIMIT,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self.config.codex.command_timeout_seconds,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    stderr.decode("utf-8", errors="replace").strip() or f"codex exited {proc.returncode}"
                )
            parsed_thread_id = thread_id
            turn_id = None
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_events.append(event)
                if event.get("type") == "thread.started":
                    parsed_thread_id = str(event.get("thread_id"))
                elif event.get("type") == "turn.started":
                    turn_id = str(event.get("turn_id")) if event.get("turn_id") else turn_id
            final_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            if not parsed_thread_id:
                raise RuntimeError("codex did not report a thread id")
            return CodexRunResult(
                thread_id=parsed_thread_id,
                turn_id=turn_id,
                final_message=final_message.strip(),
                raw_events=raw_events,
            )
        finally:
            output_path.unlink(missing_ok=True)

    def _argv(self, *, cwd: Path, thread_id: str | None, output_path: Path) -> list[str]:
        if thread_id:
            argv = [self.config.codex.bin, "exec", "resume", thread_id, "-"]
        else:
            argv = [self.config.codex.bin, "exec", "-", "-C", str(cwd)]
        if self.config.codex.model:
            argv.extend(["--model", self.config.codex.model])
        argv.extend(
            [
                "-c",
                f'approvals_reviewer="{self.config.codex.approvals_reviewer}"',
                "--json",
                "--sandbox",
                self.config.codex.sandbox,
                "--ask-for-approval",
                self.config.codex.approval_policy,
                "--output-last-message",
                str(output_path),
            ]
        )
        return argv

    async def close(self) -> None:
        return None

    async def cleanup_thread(self, thread_id: str) -> None:
        archiver = CodexAppServerBackend(self.config)
        try:
            await archiver.cleanup_thread(thread_id)
        finally:
            await archiver.close()


class CodexAppServerBackend:
    def __init__(self, config: NyanpasuConfig) -> None:
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._turn_waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._completed_turns: dict[tuple[str, str], dict[str, Any]] = {}
        self._thread_events: dict[str, list[dict[str, Any]]] = {}
        self._agent_messages: dict[tuple[str, str], list[str]] = {}
        self._start_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    async def run_turn(self, *, cwd: Path, prompt: str, thread_id: str | None) -> CodexRunResult:
        await self._ensure_started()
        if thread_id:
            thread = await self._request(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "cwd": str(cwd),
                    "approvalPolicy": self.config.codex.approval_policy,
                    "approvalsReviewer": self.config.codex.approvals_reviewer,
                    "sandbox": self.config.codex.sandbox,
                    "model": self.config.codex.model,
                },
            )
            active_thread_id = str(thread["thread"]["id"])
        else:
            thread = await self._request(
                "thread/start",
                {
                    "cwd": str(cwd),
                    "approvalPolicy": self.config.codex.approval_policy,
                    "approvalsReviewer": self.config.codex.approvals_reviewer,
                    "sandbox": self.config.codex.sandbox,
                    "model": self.config.codex.model,
                },
            )
            active_thread_id = str(thread["thread"]["id"])
        self._thread_events[active_thread_id] = []
        turn = await self._request(
            "turn/start",
            {
                "threadId": active_thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "cwd": str(cwd),
                "approvalPolicy": self.config.codex.approval_policy,
                "approvalsReviewer": self.config.codex.approvals_reviewer,
                "sandboxPolicy": self._sandbox_policy(cwd),
                "model": self.config.codex.model,
            },
        )
        turn_id = str(turn["turn"]["id"])
        key = (active_thread_id, turn_id)
        completed = self._completed_turns.pop(key, None)
        if completed is None:
            waiter = asyncio.get_running_loop().create_future()
            self._turn_waiters[key] = waiter
            try:
                completed = await asyncio.wait_for(waiter, timeout=self.config.codex.command_timeout_seconds)
            finally:
                self._turn_waiters.pop(key, None)
        final_message = self._last_agent_message(completed.get("turn", {}).get("items", []))
        if not final_message:
            final_message = "\n".join(self._agent_messages.pop(key, []))
        raw_events = self._thread_events.pop(active_thread_id, [])
        status = completed.get("turn", {}).get("status")
        if status != "completed":
            error = completed.get("turn", {}).get("error")
            raise RuntimeError(f"codex turn ended with status {status}: {error}")
        return CodexRunResult(
            thread_id=active_thread_id,
            turn_id=turn_id,
            final_message=final_message.strip(),
            raw_events=raw_events,
        )

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._reset_dead_process()
            self._proc = await asyncio.create_subprocess_exec(
                self.config.codex.bin,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=safe_codex_env(self.config),
                limit=SUBPROCESS_BUFFER_LIMIT,
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            await self._request_started(
                "initialize",
                {
                    "clientInfo": {"name": "nyanpasu", "title": "Nyanpasu", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True, "requestAttestation": False},
                },
            )
            await self._write({"method": "initialized"})

    async def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if not await self._process_alive():
            await self._ensure_started()
        return await self._request_started(method, params)

    async def _request_started(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        async with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            await self._write({"id": request_id, "method": method, "params": params})
        return await future

    async def _process_alive(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.returncode is None:
            return True
        await self._reset_dead_process()
        return False

    async def _reset_dead_process(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            return
        self._fail_pending(RuntimeError(f"codex app-server exited with {self._proc.returncode}"))
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._proc = None
        self._reader_task = None
        self._pending.clear()
        self._turn_waiters.clear()
        self._completed_turns.clear()
        self._thread_events.clear()
        self._agent_messages.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        self._proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _respond(self, request_id: int, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def _read_loop(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                self._handle_message(message)
        except Exception as exc:
            self._fail_pending(exc)
            raise

    def _handle_message(self, message: dict[str, Any]) -> None:
        thread_id = _message_thread_id(message)
        if thread_id and thread_id in self._thread_events:
            self._thread_events[thread_id].append(message)

        if "id" in message and ("result" in message or "error" in message):
            request_id = int(message["id"])
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result", {}))
            return

        method = message.get("method")
        if method == "turn/completed":
            self._handle_turn_completed(message.get("params", {}))
        elif method == "item/completed":
            self._handle_item_completed(message.get("params", {}))
        elif message.get("type") == "event_msg":
            self._handle_event_payload(message.get("payload", {}))
        elif "id" in message:
            self._handle_server_request(message)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        try:
            request_id = int(message["id"])
        except (TypeError, ValueError):
            return
        method = str(message.get("method") or "")
        if method in {"execCommandApproval", "applyPatchApproval"}:
            self._schedule_response(request_id, {"decision": "denied"})
        elif method == "item/commandExecution/requestApproval":
            self._schedule_response(request_id, {"decision": "decline"})
        elif method == "item/fileChange/requestApproval":
            self._schedule_response(request_id, {"decision": "decline"})
        elif method == "item/tool/requestUserInput":
            self._schedule_response(request_id, {"answers": {}})
        elif method == "item/tool/call":
            self._schedule_response(
                request_id,
                {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": "Dynamic tools are not available in Nyanpasu."}],
                },
            )
        elif method == "item/permissions/requestApproval":
            self._schedule_response(
                request_id,
                {
                    "permissions": {
                        "network": {"enabled": False},
                        "fileSystem": {"read": [], "write": []},
                    },
                    "scope": "turn",
                    "strictAutoReview": True,
                },
            )
        else:
            self._schedule_error(request_id, -32601, f"unsupported app-server request: {method}")

    def _schedule_response(self, request_id: int, result: dict[str, Any]) -> None:
        asyncio.create_task(self._respond(request_id, result))

    def _schedule_error(self, request_id: int, code: int, message: str) -> None:
        asyncio.create_task(self._write({"id": request_id, "error": {"code": code, "message": message}}))

    def _handle_turn_completed(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        thread_id = str(params.get("threadId"))
        turn_id = str(params.get("turn", {}).get("id"))
        if not thread_id or not turn_id:
            return
        self._complete_turn((thread_id, turn_id), params)

    def _handle_item_completed(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        item = params.get("item", {})
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            thread_id = str(params.get("threadId"))
            turn_id = str(params.get("turnId"))
            self._agent_messages.setdefault((thread_id, turn_id), []).append(str(item.get("text", "")))

    def _handle_event_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        payload_type = payload.get("type")
        if payload_type == "agent_message" and payload.get("phase") == "final_answer":
            thread_id = str(payload.get("thread_id") or payload.get("threadId") or "")
            turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
            if thread_id and turn_id:
                self._agent_messages.setdefault((thread_id, turn_id), []).append(str(payload.get("message", "")))
        elif payload_type == "task_complete":
            turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
            if not turn_id:
                return
            for thread_id in list(self._thread_events):
                key = (thread_id, turn_id)
                self._complete_turn(
                    key,
                    {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn_id,
                            "status": "completed",
                            "items": [{"type": "agentMessage", "text": payload.get("last_agent_message", "")}],
                        },
                    },
                )

    def _complete_turn(self, key: tuple[str, str], params: dict[str, Any]) -> None:
        future = self._turn_waiters.get(key)
        if future is not None and not future.done():
            future.set_result(params)
        else:
            self._completed_turns[key] = params

    def _fail_pending(self, exc: Exception) -> None:
        for pending in (self._pending, self._turn_waiters):
            for future in list(pending.values()):
                if not future.done():
                    future.set_exception(exc)
            pending.clear()

    def _last_agent_message(self, items: list[dict[str, Any]]) -> str:
        for item in reversed(items):
            if item.get("type") == "agentMessage":
                return str(item.get("text", ""))
        return ""

    def _sandbox_policy(self, cwd: Path) -> dict[str, Any]:
        if self.config.codex.sandbox == "workspace-write":
            return {
                "type": "workspaceWrite",
                "writableRoots": [str(cwd)],
                "networkAccess": True,
                "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False,
            }
        if self.config.codex.sandbox == "read-only":
            return {"type": "readOnly", "networkAccess": True}
        return {"type": "dangerFullAccess"}

    async def cleanup_thread(self, thread_id: str) -> None:
        await self._ensure_started()
        await self._request("thread/archive", {"threadId": thread_id})

    async def close(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        with anyio.move_on_after(5):
            await self._proc.wait()
        if self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()


def _message_thread_id(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("threadId"), str):
        return params["threadId"]
    result = message.get("result")
    if isinstance(result, dict):
        thread = result.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
    return None


def safe_codex_env(config: NyanpasuConfig) -> dict[str, str]:
    allowed = {
        "ALL_PROXY",
        "CODEX_HOME",
        "CODEX_NETWORK_ALLOW_LOCAL_BINDING",
        "CODEX_NETWORK_PROXY_ACTIVE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    allowed.update(config.codex.pass_env)
    return {key: value for key in allowed if (value := os.environ.get(key))}
