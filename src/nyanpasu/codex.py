from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections import deque
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from nyanpasu.diagnostics import diagnostic
from nyanpasu.environment import process_env
from nyanpasu.execution import ExecutionStarted, JsonProcessRunner, json_lines, stop_process
from nyanpasu.models import RunResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.diagnostics import Diagnostic

SUBPROCESS_BUFFER_LIMIT = 64 * 1024 * 1024


class CodexSessionSource(Protocol):
    async def read_thread(self, thread_id: str) -> dict[str, Any]: ...

    async def list_turns(self, thread_id: str, cursor: str | None = None) -> dict[str, Any]: ...


class CodexExecBackend:
    def __init__(self, config: NyanpasuConfig) -> None:
        self.config = config
        self._env = MappingProxyType(safe_codex_env(config))
        self._history = CodexAppServerBackend(config, env=self._env)
        self._runner = JsonProcessRunner()

    def runtime_info(self) -> dict:
        return self._runner.runtime_info()

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as output_file:
            output_path = Path(output_file.name)
        argv = self._argv(
            cwd=cwd,
            thread_id=thread_id,
            output_path=output_path,
            developer_instructions=developer_instructions,
        )
        try:
            parsed_thread_id, turn_id = thread_id, None

            async def received(event: dict[str, Any]) -> None:
                nonlocal parsed_thread_id, turn_id
                if event.get("type") == "thread.started":
                    parsed_thread_id = event.get("thread_id")
                if event.get("type") == "turn.started":
                    turn_id = event.get("turn_id")
                if event.get("type") in {"thread.started", "turn.started"} and parsed_thread_id and on_started:
                    await on_started(parsed_thread_id, turn_id)

            returncode, stderr_tail = await self._runner.run(
                argv,
                cwd=cwd,
                env=self._env,
                input_text=prompt,
                timeout=self.config.codex.command_timeout_seconds,
                received=received,
            )
            if returncode != 0:
                raise RuntimeError(stderr_tail or f"codex exited {returncode}")
            final_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            if not parsed_thread_id:
                raise RuntimeError("codex did not report a thread id")
            return RunResult(
                thread_id=parsed_thread_id,
                turn_id=turn_id,
                final_message=final_message.strip(),
            )
        finally:
            output_path.unlink(missing_ok=True)

    def _argv(
        self, *, cwd: Path, thread_id: str | None, output_path: Path, developer_instructions: str = ""
    ) -> list[str]:
        if thread_id:
            argv = [*self.config.codex.command, "exec", "resume", thread_id, "-"]
        else:
            argv = [*self.config.codex.command, "exec", "-", "-C", str(cwd)]
        if self.config.codex.model:
            argv.extend(["--model", self.config.codex.model])
        if self.config.codex.reasoning_effort:
            argv.extend(["-c", f"model_reasoning_effort={json.dumps(self.config.codex.reasoning_effort)}"])
        if developer_instructions:
            argv.extend(["-c", f"developer_instructions={json.dumps(developer_instructions, ensure_ascii=False)}"])
        argv.extend(
            [
                "-c",
                f'approvals_reviewer="{self.config.codex.approvals_reviewer}"',
                "-c",
                f'sandbox_mode="{self.config.codex.sandbox}"',
                "-c",
                f'approval_policy="{self.config.codex.approval_policy}"',
                "--json",
                "--output-last-message",
                str(output_path),
            ]
        )
        return argv

    async def close(self) -> None:
        await self._runner.close()
        await self._history.close()

    async def cleanup_thread(self, thread_id: str) -> None:
        await self._history.cleanup_thread(thread_id)

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        return await self._history.read_thread(thread_id)

    async def list_turns(self, thread_id: str, cursor: str | None = None) -> dict[str, Any]:
        return await self._history.list_turns(thread_id, cursor)


class CodexAppServerBackend:
    def __init__(self, config: NyanpasuConfig, *, env: Mapping[str, str] | None = None) -> None:
        self.config = config
        self._env = MappingProxyType(safe_codex_env(config) if env is None else dict(env))
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._turn_waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._completed_turns: dict[tuple[str, str], dict[str, Any]] = {}
        self._agent_messages: dict[tuple[str, str], list[str]] = {}
        self._start_lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self.diagnostics: deque[Diagnostic] = deque(maxlen=100)

    def runtime_info(self) -> dict:
        return {
            "connection": "connected" if self._proc is not None and self._proc.returncode is None else "idle",
            "diagnostics": list(self.diagnostics),
        }

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult:
        key: tuple[str, str] | None = None
        try:
            await self._ensure_started()
            thread_params: dict[str, Any] = {
                "cwd": str(cwd),
                "approvalPolicy": self.config.codex.approval_policy,
                "approvalsReviewer": self.config.codex.approvals_reviewer,
                "sandbox": self.config.codex.sandbox,
                "model": self.config.codex.model,
            }
            if thread_id:
                thread_params["threadId"] = thread_id
            if developer_instructions:
                thread_params["developerInstructions"] = developer_instructions
            if self.config.codex.reasoning_effort:
                thread_params["config"] = {"model_reasoning_effort": self.config.codex.reasoning_effort}
            thread = await self._request("thread/resume" if thread_id else "thread/start", thread_params)
            active_thread_id = str(thread["thread"]["id"])
            if on_started:
                await on_started(active_thread_id, None)
            start = asyncio.create_task(
                self._request(
                    "turn/start",
                    {
                        "threadId": active_thread_id,
                        "input": [{"type": "text", "text": prompt, "text_elements": []}],
                        "cwd": str(cwd),
                        "approvalPolicy": self.config.codex.approval_policy,
                        "approvalsReviewer": self.config.codex.approvals_reviewer,
                        "sandboxPolicy": self._sandbox_policy(cwd),
                        "model": self.config.codex.model,
                        "effort": self.config.codex.reasoning_effort,
                    },
                )
            )
            try:
                turn = await asyncio.shield(start)
            except asyncio.CancelledError:
                turn = await start
                await self._interrupt_turn((active_thread_id, str(turn["turn"]["id"])))
                raise
            turn_id = str(turn["turn"]["id"])
            key = (active_thread_id, turn_id)
            if on_started:
                await on_started(active_thread_id, turn_id)
            completed = self._completed_turns.pop(key, None)
            if completed is None:
                waiter = asyncio.get_running_loop().create_future()
                self._turn_waiters[key] = waiter
                try:
                    completed = await asyncio.wait_for(
                        asyncio.shield(waiter), timeout=self.config.codex.command_timeout_seconds
                    )
                except (asyncio.CancelledError, TimeoutError):
                    await self._interrupt_turn(key)
                    key = None
                    raise
                finally:
                    self._turn_waiters.pop((active_thread_id, turn_id), None)
            final_message = self._last_agent_message(completed.get("turn", {}).get("items", []))
            messages = self._agent_messages.pop(key, [])
            if not final_message:
                final_message = "\n".join(messages)
            status = completed.get("turn", {}).get("status")
            if status != "completed":
                error = completed.get("turn", {}).get("error")
                raise RuntimeError(f"codex turn ended with status {status}: {error}")
            return RunResult(
                thread_id=active_thread_id,
                turn_id=turn_id,
                final_message=final_message.strip(),
            )
        except asyncio.CancelledError:
            if key is not None:
                await self._interrupt_turn(key)
            raise
        finally:
            if key is not None:
                self._turn_waiters.pop(key, None)
                self._agent_messages.pop(key, None)

    async def _interrupt_turn(self, key: tuple[str, str]) -> None:
        # The RPC acknowledgement is not execution completion. Keep the workspace
        # until the server has emitted turn/completed as well.
        if self._completed_turns.pop(key, None) is not None:
            return
        waiter = self._turn_waiters.setdefault(key, asyncio.get_running_loop().create_future())
        try:
            if not waiter.done():
                await self._request("turn/interrupt", {"threadId": key[0], "turnId": key[1]})
            await asyncio.wait_for(asyncio.shield(waiter), timeout=30)
        except Exception as exc:
            raise RuntimeError("Cannot confirm Codex stopped; preserve its workspace") from exc
        finally:
            self._turn_waiters.pop(key, None)
            self._agent_messages.pop(key, None)

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = await self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        return result["thread"]

    async def list_turns(self, thread_id: str, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "itemsView": "full",
            "sortDirection": "asc",
            "limit": 50,
        }
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("thread/turns/list", params)

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._reset_dead_process()
            self._proc = await asyncio.create_subprocess_exec(
                *self.config.codex.command,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                limit=SUBPROCESS_BUFFER_LIMIT,
                start_new_session=os.name == "posix",
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self._request_started(
                "initialize",
                {
                    "clientInfo": {"name": "nyanpasu", "title": "Nyanpasu", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True, "requestAttestation": False},
                },
            )
            await self._write({"method": "initialized"})

    async def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        await self._ensure_started()
        return await self._request_started(method, params)

    async def _request_started(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {"id": request_id, "method": method, "params": params}
        try:
            await self._write(request)
            return await asyncio.wait_for(future, self.config.codex.command_timeout_seconds)
        finally:
            self._pending.pop(request_id, None)

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
        await stop_process(self._proc)
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._proc = None
        self._reader_task = None
        self._pending.clear()
        self._turn_waiters.clear()
        self._completed_turns.clear()
        self._agent_messages.clear()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    async def _write(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        self._proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _respond(self, request_id: int, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        async for line in self._proc.stderr:
            self.diagnostics.append(diagnostic(line.decode("utf-8", "replace")))

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for message in json_lines(self._proc.stdout):
                self._handle_message(message)
            self._fail_pending(RuntimeError("codex app-server closed its output stream"))
        except (OSError, ValueError) as exc:
            self._fail_pending(exc)

    def _handle_message(self, message: dict[str, Any]) -> None:
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
            explicit_thread = payload.get("thread_id") or payload.get("threadId")
            candidates = (
                [explicit_thread] if explicit_thread else [key[0] for key in self._turn_waiters if key[1] == turn_id]
            )
            if len(candidates) != 1:
                return
            for thread_id in candidates:
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
        await stop_process(self._proc)
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        self._fail_pending(RuntimeError("codex app-server closed"))


def safe_codex_env(config: NyanpasuConfig) -> dict[str, str]:
    return process_env(config.codex, cwd=config.state_dir, backend="codex")
