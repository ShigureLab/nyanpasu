from __future__ import annotations

import json
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from nyanpasu.environment import process_env
from nyanpasu.execution import ExecutionStarted, JsonProcessRunner
from nyanpasu.models import RunResult
from nyanpasu.transcript.content import redact

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.config import NyanpasuConfig


class ClaudeBackend:
    """One print-mode process per turn; Claude owns persistence and resume."""

    def __init__(self, config: NyanpasuConfig):
        self.config = config.claude
        self.env = MappingProxyType(process_env(self.config, cwd=config.state_dir, backend="claude"))
        self._runner = JsonProcessRunner()

    def runtime_info(self) -> dict:
        return self._runner.runtime_info()

    def _argv(self, session_id: str, *, resume: bool, instructions: str) -> list[str]:
        argv = [
            *self.config.command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--replay-user-messages",
            "--permission-mode",
            self.config.permission_mode,
            "--permission-prompts",
            "none",
            "--system-prompt-snapshot",
            "off",
            "--resume" if resume else "--session-id",
            session_id,
        ]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.reasoning_effort:
            argv.extend(["--effort", self.config.reasoning_effort])
        if self.config.allowed_tools:
            argv.extend(["--allowedTools", ",".join(self.config.allowed_tools)])
        if instructions:
            argv.extend(["--append-system-prompt", instructions])
        return argv

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
    ) -> RunResult:
        session_id = str(UUID(thread_id)) if thread_id else str(uuid4())
        # The input message UUID is persisted by Claude, so task links survive restarts.
        turn_id = str(uuid4())
        result = None

        async def received(event: dict) -> None:
            nonlocal result
            if event.get("type") == "system" and event.get("subtype") == "init":
                if event.get("session_id") != session_id:
                    raise RuntimeError("Claude reported a different session ID")
                if on_started:
                    await on_started(session_id, turn_id)
            elif event.get("type") == "result":
                result = event

        returncode, stderr = await self._runner.run(
            self._argv(session_id, resume=thread_id is not None, instructions=developer_instructions),
            cwd=cwd,
            env=self.env,
            timeout=self.config.command_timeout_seconds,
            input_text=json.dumps(
                {
                    "type": "user",
                    "session_id": session_id,
                    "uuid": turn_id,
                    "message": {"role": "user", "content": prompt},
                },
                ensure_ascii=False,
            )
            + "\n",
            received=received,
        )
        if result is None:
            raise RuntimeError(redact(stderr) or f"Claude exited {returncode} without a result")
        if result.get("session_id") != session_id:
            raise RuntimeError("Claude result belongs to a different session")
        if returncode or result.get("is_error") or result.get("subtype") != "success":
            reason = result.get("result") or "; ".join(result.get("errors", [])) or stderr or result.get("subtype")
            raise RuntimeError(f"Claude run failed: {redact(reason)}")
        if not isinstance(result.get("result"), str):
            raise RuntimeError("Claude success result is missing its message")
        return RunResult(thread_id=session_id, turn_id=turn_id, final_message=result["result"])

    async def cleanup_thread(self, thread_id: str) -> None:
        # There is no archive RPC. Keep Claude's history available after task cleanup.
        pass

    async def close(self) -> None:
        await self._runner.close()
