from __future__ import annotations

import json
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from nyanpasu.config import ClaudeBackendConfig, ModelSettings
from nyanpasu.diagnostics import diagnostic
from nyanpasu.environment import process_env
from nyanpasu.execution import ExecutionStarted, JsonProcessRunner
from nyanpasu.models import RunResult
from nyanpasu.native_home import native_env
from nyanpasu.redaction import redact

if TYPE_CHECKING:
    from pathlib import Path

    from nyanpasu.config import FallbackModel, NyanpasuConfig
    from nyanpasu.targets import ExecutionTarget


class AutoReviewUnavailable(RuntimeError):
    def __init__(self, session_id: str, model: str | None, reason: str):
        super().__init__(f"Claude auto review unavailable: {redact(reason)}")
        self.session_id = session_id
        self.model = model


def auto_review_failure(event: dict) -> str | None:
    if event.get("type") != "user":
        return None
    blocks = event.get("message", {}).get("content", [])
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if block.get("type") != "tool_result" or not block.get("is_error"):
            continue
        value = block.get("content", "")
        text = value if isinstance(value, str) else "\n".join(part.get("text", "") for part in value)
        if event.get("toolDenialKind") == "automode-unavailable" or re.match(
            r"^[^\n]+ is temporarily unavailable(?: \([^)]*\))?, so auto mode cannot determine the safety of ", text
        ):
            return text
    return None


class ClaudeBackend:
    """One print-mode process per turn; Claude owns persistence and resume."""

    def __init__(self, config: NyanpasuConfig, name: str = "claude", *, home: Path | None = None):
        configured = config.backends[name]
        if not isinstance(configured, ClaudeBackendConfig):
            raise ValueError(f"backend {name} is not a Claude Code backend")
        self.config = configured
        env = native_env(process_env(configured.process, cwd=config.state_dir, backend="claude-code"), home=home)
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        self.env = MappingProxyType(env)
        self._runner = JsonProcessRunner()

    def runtime_info(self) -> dict:
        return {
            **self._runner.runtime_info(),
            "fallback_models": [model.model_dump() for model in self.config.options.fallback_models],
        }

    def _argv(
        self,
        session_id: str,
        *,
        resume: bool,
        instructions: str,
        settings: ModelSettings,
        fallbacks: tuple[FallbackModel, ...],
        output_schema: dict[str, Any] | None = None,
    ) -> list[str]:
        options = self.config.options
        argv = [
            *self.config.process.command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--replay-user-messages",
            "--permission-mode",
            options.permission_mode,
            "--resume" if resume else "--session-id",
            session_id,
        ]
        if settings.model:
            argv.extend(["--model", settings.model])
        if fallbacks:
            argv.extend(["--fallback-model", ",".join(model.model for model in fallbacks)])
        if any(model.reasoning is not None for model in fallbacks):
            model_settings = {
                model.model.removesuffix("[1m]"): {"effortLevel": model.reasoning or settings.reasoning}
                for model in fallbacks
                if model.reasoning is not None or settings.reasoning is not None
            }
            if settings.model and settings.reasoning:
                model_settings[settings.model.removesuffix("[1m]")] = {"effortLevel": settings.reasoning}
            native_settings: dict = {"modelSettings": model_settings}
            if settings.reasoning:
                native_settings["effortLevel"] = settings.reasoning
            argv.extend(["--settings", json.dumps(native_settings)])
        elif settings.reasoning:
            argv.extend(["--effort", settings.reasoning])
        if options.allowed_tools:
            argv.extend(["--allowedTools", ",".join(options.allowed_tools)])
        if instructions:
            argv.extend(["--append-system-prompt", instructions])
        if output_schema is not None:
            argv.extend(["--json-schema", json.dumps(output_schema)])
        return argv

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        execution: ExecutionTarget,
        developer_instructions: str = "",
        on_started: ExecutionStarted | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> RunResult:
        choices = (
            ModelSettings(model=execution.model, reasoning=execution.reasoning),
            *self.config.options.fallback_models,
        )
        if any(choice.reasoning is not None for choice in choices[1:]) and execution.reasoning not in {
            None,
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ValueError("Claude per-model fallback reasoning supports low, medium, high, and xhigh")
        index = 0
        while True:
            choice = choices[index]
            settings = ModelSettings(model=choice.model, reasoning=choice.reasoning or execution.reasoning)
            try:
                return await self._run_turn(
                    cwd=cwd,
                    prompt=prompt,
                    thread_id=thread_id,
                    developer_instructions=developer_instructions,
                    on_started=on_started,
                    settings=settings,
                    fallbacks=self.config.options.fallback_models[index:],
                    timeout=execution.turn_timeout_seconds,
                    output_schema=output_schema,
                )
            except AutoReviewUnavailable as exc:
                # Changing generation models cannot recover a pinned classifier.
                if self.env.get("CLAUDE_CODE_AUTO_MODE_MODEL"):
                    raise
                # A native generation fallback may already have advanced the chain.
                for i in range(index, len(choices)):
                    if (choices[i].model or "").removesuffix("[1m]") == (exc.model or "").removesuffix("[1m]"):
                        index = i
                        break
                index += 1
                if index == len(choices):
                    raise
                thread_id = exc.session_id
                prompt = (
                    "The previous turn was interrupted because the auto-mode safety classifier was unavailable. "
                    "Continue the existing task from the saved conversation and workspace. "
                    "Check completed actions before repeating them. Retry the blocked action through auto mode; "
                    "the earlier failure was not an approval."
                )
                self._runner.diagnostics.append(
                    {
                        **diagnostic(f"{exc}; resuming with {choices[index].model}; session_id={thread_id}"),
                        "level": "warn",
                        "target": "claude.model_fallback",
                    }
                )

    async def _run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None,
        developer_instructions: str,
        on_started: ExecutionStarted | None,
        settings: ModelSettings,
        fallbacks: tuple[FallbackModel, ...],
        timeout: int,
        output_schema: dict[str, Any] | None = None,
    ) -> RunResult:
        session_id = str(UUID(thread_id)) if thread_id else str(uuid4())
        # The input message UUID is persisted by Claude, so task links survive restarts.
        turn_id = str(uuid4())
        result = None
        active_model = settings.model

        async def received(event: dict) -> None:
            nonlocal result, active_model
            if event.get("type") == "system" and event.get("subtype") == "init":
                if event.get("session_id") != session_id:
                    raise RuntimeError("Claude reported a different session ID")
                if on_started:
                    await on_started(session_id, turn_id)
                active_model = event.get("model", active_model)
            elif event.get("type") == "system" and event.get("subtype") == "model_fallback":
                active_model = event["fallback_model"]
                self._runner.diagnostics.append(
                    {
                        **diagnostic(json.dumps(redact(event), ensure_ascii=False)),
                        "level": "warn",
                        "target": "claude.model_fallback",
                    }
                )
            elif event.get("type") == "result":
                result = event
            elif self.config.options.permission_mode == "auto" and (reason := auto_review_failure(event)):
                failure = AutoReviewUnavailable(session_id, active_model, reason)
                self._runner.diagnostics.append(
                    {
                        **diagnostic(f"{failure}; session_id={session_id}; turn_id={turn_id}"),
                        "level": "warn",
                        "target": "claude.auto_review_unavailable",
                    }
                )
                raise failure

        argv = self._argv(
            session_id,
            resume=thread_id is not None,
            instructions=developer_instructions,
            settings=settings,
            fallbacks=fallbacks,
            output_schema=output_schema,
        )
        try:
            returncode, stderr = await self._runner.run(
                argv,
                cwd=cwd,
                env=self.env,
                timeout=timeout,
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
        except* AutoReviewUnavailable as failures:
            # The stdout callback is the sole source; the process has stopped before resuming.
            raise failures.exceptions[0] from None
        if result is None:
            raise RuntimeError(redact(stderr) or f"Claude exited {returncode} without a result")
        if result.get("session_id") != session_id:
            raise RuntimeError("Claude result belongs to a different session")
        if returncode or result.get("is_error") or result.get("subtype") != "success":
            reason = result.get("result") or "; ".join(result.get("errors", [])) or stderr or result.get("subtype")
            raise RuntimeError(f"Claude run failed: {redact(reason)}")
        if output_schema is not None:
            if "structured_output" not in result:
                raise RuntimeError("Claude success result is missing its structured output")
            final_message = json.dumps(result["structured_output"], ensure_ascii=False)
        elif not isinstance(result.get("result"), str):
            raise RuntimeError("Claude success result is missing its message")
        else:
            final_message = result["result"]
        return RunResult(thread_id=session_id, turn_id=turn_id, final_message=final_message)

    async def cleanup_thread(self, thread_id: str) -> None:
        # There is no archive RPC. Keep Claude's history available after task cleanup.
        pass

    async def close(self) -> None:
        await self._runner.close()
