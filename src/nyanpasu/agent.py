from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio.to_thread as to_thread
from loguru import logger

from nyanpasu.codex import CodexBackend, backend_from_config
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentContext, AgentTask, TaskAction, TaskRunResult, TaskStatus, json_dumps
from nyanpasu.store import StateStore, replace_context

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig

PostProcessHook = Callable[[AgentTask, TaskRunResult], Awaitable[None]]


class WorktreeBackend(Protocol):
    def prepare_context(self, task: AgentTask, existing: AgentContext | None) -> AgentContext: ...

    def prepare_event_snapshot(self, task: AgentTask) -> Path | None: ...

    def remove_worktree(self, workspace, path: Path | None) -> None: ...


class AgentService:
    def __init__(
        self,
        config: NyanpasuConfig,
        *,
        store: StateStore | None = None,
        worktrees: WorktreeBackend | None = None,
        codex: CodexBackend | None = None,
    ) -> None:
        self.config = config
        self.store = store or StateStore(config.db_path)
        self.worktrees = worktrees or WorktreeManager(config)
        self.codex = codex or backend_from_config(config)
        self._semaphore = asyncio.Semaphore(config.runtime.concurrency)
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._submit_lock = asyncio.Lock()
        self._post_process_hooks: dict[str, list[PostProcessHook]] = {}
        self._owner_id = f"{os.uname().nodename}:{os.getpid()}:{id(self)}"

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        async with self._submit_lock:
            logger.info(
                "task submit received task_id={} action={} context={}",
                task.task_id,
                task.action.value,
                task.context_key,
            )
            is_new = await to_thread.run_sync(self.store.record_task, task)
            if not is_new:
                logger.info("task submit skipped duplicate task_id={} key={}", task.task_id, task.key)
                return {"accepted": False, "duplicate": True, "task_id": task.task_id}
            if task.action is TaskAction.IGNORED:
                result = TaskRunResult(
                    task_id=task.task_id,
                    status=TaskStatus.COMPLETED,
                    thread_id=None,
                    turn_id=None,
                    final_message="",
                    raw_events=[],
                )
                await to_thread.run_sync(self.store.mark_task_done, result)
                logger.info("task submit ignored task_id={} context={}", task.task_id, task.context_key)
                return {"accepted": True, "ignored": True, "task_id": task.task_id}
            active = await to_thread.run_sync(
                functools.partial(
                    self.store.active_task_for_context,
                    task.context_key,
                    exclude_task_id=task.task_id,
                    since=time.time() - self.config.runtime.coalesce_window_seconds,
                    statuses=(TaskStatus.QUEUED.value,),
                )
            )
            if active is not None and task.action is TaskAction.RUN:
                active_task_id = active.task_id
                await to_thread.run_sync(self.store.mark_task_coalesced, task.task_id, active_task_id)
                logger.info(
                    "task submit coalesced task_id={} into={} context={}",
                    task.task_id,
                    active_task_id,
                    task.context_key,
                )
                return {
                    "accepted": True,
                    "coalesced": True,
                    "task_id": task.task_id,
                    "coalesced_into": active_task_id,
                }
            runner = asyncio.create_task(self._run_task_guarded(task))
            self._tasks.add(runner)
            runner.add_done_callback(self._tasks.discard)
            logger.info("task submit queued task_id={} context={}", task.task_id, task.context_key)
            return {"accepted": True, "task_id": task.task_id, "action": task.action.value}

    async def run_now(self, task: AgentTask) -> TaskRunResult:
        logger.info(
            "task run_now received task_id={} action={} context={}",
            task.task_id,
            task.action.value,
            task.context_key,
        )
        is_new = await to_thread.run_sync(self.store.record_task, task)
        if not is_new:
            logger.info("task run_now duplicate task_id={} key={}", task.task_id, task.key)
            raise ValueError(f"duplicate task id or dedupe key: {task.key}")
        if task.action is TaskAction.IGNORED:
            result = TaskRunResult(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                thread_id=None,
                turn_id=None,
                final_message="",
                raw_events=[],
            )
            await to_thread.run_sync(self.store.mark_task_done, result)
            return result
        try:
            return await self._run_task(task)
        except Exception as exc:
            logger.exception("task run_now failed task_id={} context={}", task.task_id, task.context_key)
            await to_thread.run_sync(self.store.mark_task_failed, task.task_id, f"{exc}\n{traceback.format_exc()}")
            raise

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None:
        self._post_process_hooks.setdefault(plugin_id, []).append(hook)

    async def _run_task_guarded(self, task: AgentTask) -> None:
        async with self._semaphore:
            try:
                await self._run_task(task)
            except Exception as exc:
                logger.exception("task failed task_id={} context={}", task.task_id, task.context_key)
                await to_thread.run_sync(self.store.mark_task_failed, task.task_id, f"{exc}\n{traceback.format_exc()}")

    async def _run_task(self, task: AgentTask) -> TaskRunResult:
        if task.action is TaskAction.CLEANUP:
            return await self._cleanup_context(task)
        if task.action is not TaskAction.RUN:
            raise ValueError(f"unsupported task action: {task.action}")

        context_lock = self._lock_for_context(task.context_key)
        async with context_lock:
            await self._acquire_context_lease(task)
            heartbeat = asyncio.create_task(self._heartbeat_context_lease(task))
            started_at = time.monotonic()
            try:
                await to_thread.run_sync(self.store.mark_task_running, task.task_id, None)
                task = await to_thread.run_sync(self._with_coalesced_task_context, task)
                existing = await to_thread.run_sync(self.store.get_context, task.context_key)
                context = await to_thread.run_sync(self.worktrees.prepare_context, task, existing)
                if context.session_worktree is None:
                    context = replace_context(context, session_worktree=Path.cwd())
                event_worktree = None
                if task.workspace_policy == "event_snapshot":
                    event_worktree = await to_thread.run_sync(self.worktrees.prepare_event_snapshot, task)
                    await to_thread.run_sync(self.store.mark_task_running, task.task_id, event_worktree)
                logger.info(
                    "task started task_id={} context={} thread_id={} workspace={} workspace_policy={}",
                    task.task_id,
                    task.context_key,
                    context.thread_id,
                    context.session_worktree,
                    task.workspace_policy,
                )
                prompt = self._runtime_prompt(
                    task,
                    event_worktree=event_worktree,
                    session_worktree=context.session_worktree,
                )
                result = await self.codex.run_turn(
                    cwd=context.session_worktree or Path.cwd(),
                    prompt=prompt,
                    thread_id=context.thread_id,
                )
                context = replace_context(
                    context,
                    thread_id=result.thread_id,
                    revision=task.workspace.revision if task.workspace else context.revision,
                )
                await to_thread.run_sync(self.store.upsert_context, context)
                run_result = TaskRunResult(
                    task_id=task.task_id,
                    status=TaskStatus.COMPLETED,
                    thread_id=result.thread_id,
                    turn_id=result.turn_id,
                    final_message=result.final_message,
                    raw_events=result.raw_events,
                    event_worktree=event_worktree,
                    session_worktree=context.session_worktree,
                )
                await to_thread.run_sync(self.store.mark_task_done, run_result)
                logger.info(
                    "task finished task_id={} context={} thread_id={} turn_id={} elapsed_sec={:.2f}",
                    task.task_id,
                    task.context_key,
                    result.thread_id,
                    result.turn_id,
                    time.monotonic() - started_at,
                )
                await self._run_post_process_hooks(task, run_result)
                if task.workspace_policy == "event_snapshot" and self.config.runtime.clean_event_snapshots:
                    await to_thread.run_sync(self.worktrees.remove_worktree, task.workspace, event_worktree)
                    logger.info("task event snapshot removed task_id={} path={}", task.task_id, event_worktree)
                return run_result
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await to_thread.run_sync(
                    functools.partial(
                        self.store.release_context_lease,
                        task.context_key,
                        owner_id=self._owner_id,
                        task_id=task.task_id,
                    )
                )

    async def _cleanup_context(self, task: AgentTask) -> TaskRunResult:
        context_lock = self._lock_for_context(task.context_key)
        async with context_lock:
            await self._acquire_context_lease(task)
            heartbeat = asyncio.create_task(self._heartbeat_context_lease(task))
            logger.info("task cleanup started task_id={} context={}", task.task_id, task.context_key)
            try:
                await to_thread.run_sync(self.store.mark_task_running, task.task_id, None)
                context = await to_thread.run_sync(self.store.delete_context, task.context_key)
                if context is not None:
                    if context.thread_id:
                        await self.codex.cleanup_thread(context.thread_id)
                    await to_thread.run_sync(self.worktrees.remove_worktree, task.workspace, context.session_worktree)
                result = TaskRunResult(
                    task_id=task.task_id,
                    status=TaskStatus.COMPLETED,
                    thread_id=context.thread_id if context else None,
                    turn_id=None,
                    final_message="",
                    raw_events=[],
                    session_worktree=context.session_worktree if context else None,
                )
                await to_thread.run_sync(self.store.mark_task_done, result)
                logger.info(
                    "task cleanup finished task_id={} context={} had_context={}",
                    task.task_id,
                    task.context_key,
                    context is not None,
                )
                await self._run_post_process_hooks(task, result)
                return result
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await to_thread.run_sync(
                    functools.partial(
                        self.store.release_context_lease,
                        task.context_key,
                        owner_id=self._owner_id,
                        task_id=task.task_id,
                    )
                )

    async def _run_post_process_hooks(self, task: AgentTask, result: TaskRunResult) -> None:
        plugin_id = task.metadata.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            return
        for hook in self._post_process_hooks.get(plugin_id, []):
            await hook(task, result)

    def _with_coalesced_task_context(self, task: AgentTask) -> AgentTask:
        coalesced = self.store.coalesced_tasks_for(task.task_id)
        if not coalesced:
            return task
        metadata = dict(task.metadata)
        metadata["coalesced_tasks"] = [item.model_dump(mode="json") for item in coalesced]
        return task.model_copy(update={"metadata": metadata})

    def _runtime_prompt(
        self,
        task: AgentTask,
        *,
        event_worktree: Path | None,
        session_worktree: Path | None,
    ) -> str:
        prompt = task.prompt.replace(
            "{{NYANPASU_EVENT_WORKTREE}}",
            str(event_worktree or session_worktree or Path.cwd()),
        ).replace(
            "{{NYANPASU_WORKTREE}}",
            str(event_worktree or session_worktree or Path.cwd()),
        )
        coalesced = task.metadata.get("coalesced_tasks")
        if isinstance(coalesced, list) and coalesced:
            prompt += "\n\nAdditional coalesced task context:\n"
            prompt += json_dumps(coalesced)
            prompt += "\n"
        if task.instruction_docs:
            prompt += "\n\nTask-specific instruction documents:\n"
            for doc in task.instruction_docs:
                prompt += f"\n--- {doc.name}"
                if doc.source:
                    prompt += f" ({doc.source})"
                prompt += f" ---\n{doc.content.strip()}\n"
        return prompt

    async def _acquire_context_lease(self, task: AgentTask) -> None:
        waited = False
        while True:
            acquired = await to_thread.run_sync(
                functools.partial(
                    self.store.try_acquire_context_lease,
                    task.context_key,
                    owner_id=self._owner_id,
                    task_id=task.task_id,
                    ttl_seconds=self.config.runtime.context_lease_seconds,
                )
            )
            if acquired:
                if waited:
                    logger.info("context lease acquired task_id={} context={}", task.task_id, task.context_key)
                return
            if not waited:
                logger.info(
                    "context lease waiting task_id={} context={} wait_sec={}",
                    task.task_id,
                    task.context_key,
                    self.config.runtime.context_lease_wait_seconds,
                )
                waited = True
            await asyncio.sleep(self.config.runtime.context_lease_wait_seconds)

    async def _heartbeat_context_lease(self, task: AgentTask) -> None:
        interval = self.config.runtime.context_lease_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            ok = await to_thread.run_sync(
                functools.partial(
                    self.store.heartbeat_context_lease,
                    task.context_key,
                    owner_id=self._owner_id,
                    task_id=task.task_id,
                    ttl_seconds=self.config.runtime.context_lease_seconds,
                )
            )
            if not ok:
                logger.warning("context lease heartbeat lost task_id={} context={}", task.task_id, task.context_key)
                return

    def _lock_for_context(self, key: str) -> asyncio.Lock:
        lock = self._context_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._context_locks[key] = lock
        return lock

    async def shutdown(self) -> None:
        logger.info("agent shutdown started active_tasks={}", len(self._tasks))
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.codex.close()
        logger.info("agent shutdown finished")
