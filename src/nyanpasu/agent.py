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

from nyanpasu.backends import Backends
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentContext, AgentTask, TaskAction, TaskRunResult, TaskRunSummary, TaskStatus
from nyanpasu.store import StateStore, replace_context

if TYPE_CHECKING:
    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.plugins import TaskPreparer

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
        backends: Backends | None = None,
    ) -> None:
        self.config = config
        self.store = store or StateStore(config.db_path)
        self.worktrees = worktrees or WorktreeManager(config)
        self.backends = backends or Backends(config)
        self._semaphore = asyncio.Semaphore(config.runtime.concurrency)
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._submit_lock = asyncio.Lock()
        self._post_process_hooks: dict[str, list[PostProcessHook]] = {}
        self._task_preparers: dict[str, TaskPreparer] = {}
        self._owner_id = f"{os.uname().nodename}:{os.getpid()}:{id(self)}"

    async def startup(self) -> None:
        async with self._submit_lock:
            for task in await to_thread.run_sync(self.store.unfinished_tasks):
                plugin_id = task.metadata.get("plugin_id")
                if plugin_id and plugin_id not in (self.config.enabled_plugins or self.config.plugins):
                    logger.warning(
                        "task recovery deferred: plugin disabled task_id={} plugin={}", task.task_id, plugin_id
                    )
                    continue
                self._preparer_for(task)
                self._schedule(task)
                logger.info("task recovery scheduled task_id={} context={}", task.task_id, task.context_key)

    def _schedule(self, task: AgentTask) -> None:
        if any(runner.get_name() == task.task_id for runner in self._tasks):
            return
        runner = asyncio.create_task(self._run_task_guarded(task), name=task.task_id)
        self._tasks.add(runner)
        runner.add_done_callback(self._tasks.discard)

    async def submit(self, task: AgentTask) -> dict[str, Any]:
        async with self._submit_lock:
            logger.info(
                "task submit received task_id={} action={} context={}",
                task.task_id,
                task.action.value,
                task.context_key,
            )
            coalesce_since = time.time() - self.config.runtime.coalesce_window_seconds if task.coalesce_key else None
            self._preparer_for(task)
            is_new, active_task_id = await to_thread.run_sync(
                functools.partial(
                    self.store.enqueue_task,
                    task,
                    default_backend=self.config.runtime.backend,
                    coalesce_since=coalesce_since,
                )
            )
            if not is_new:
                logger.info("task submit skipped duplicate task_id={} key={}", task.task_id, task.key)
                return {"accepted": False, "duplicate": True, "task_id": task.task_id}
            if task.action is TaskAction.IGNORED:
                await self._complete_ignored_task(task)
                logger.info("task submit ignored task_id={} context={}", task.task_id, task.context_key)
                return {"accepted": True, "ignored": True, "task_id": task.task_id}
            if active_task_id is not None:
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
            self._schedule(task)
            logger.info("task submit queued task_id={} context={}", task.task_id, task.context_key)
            return {"accepted": True, "task_id": task.task_id, "action": task.action.value}

    async def run_now(self, task: AgentTask) -> TaskRunResult:
        logger.info(
            "task run_now received task_id={} action={} context={}",
            task.task_id,
            task.action.value,
            task.context_key,
        )
        is_new = await to_thread.run_sync(
            functools.partial(self.store.record_task, task, default_backend=self.config.runtime.backend)
        )
        if not is_new:
            logger.info("task run_now duplicate task_id={} key={}", task.task_id, task.key)
            raise ValueError(f"duplicate task id or dedupe key: {task.key}")
        if task.action is TaskAction.IGNORED:
            return await self._complete_ignored_task(task)
        try:
            result = await self._run_task(task)
        except Exception:
            logger.exception("task run_now failed task_id={} context={}", task.task_id, task.context_key)
            raise
        if result is None:
            raise ValueError(f"task already handled by another worker: {task.key}")
        return result

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None:
        self._post_process_hooks.setdefault(plugin_id, []).append(hook)

    async def _complete_ignored_task(self, task: AgentTask) -> TaskRunResult:
        result = TaskRunResult(
            task_id=task.task_id,
            status=TaskStatus.COMPLETED,
            backend=await to_thread.run_sync(self.store.task_backend, task.task_id),
            thread_id=None,
            turn_id=None,
            final_message="",
        )
        await to_thread.run_sync(self.store.mark_task_done, result)
        return result

    def add_task_preparer(self, plugin_id: str, preparer: TaskPreparer) -> None:
        self._task_preparers[plugin_id] = preparer

    async def _run_task_guarded(self, task: AgentTask) -> None:
        try:
            await self._run_task(task)
        except asyncio.CancelledError:
            logger.info("task cancelled task_id={} context={}", task.task_id, task.context_key)
            raise
        except Exception:
            logger.exception("task failed task_id={} context={}", task.task_id, task.context_key)

    async def _run_task(self, task: AgentTask) -> TaskRunResult | None:
        # Waiting for a busy context must not reserve capacity needed by other contexts.
        async with self._context_execution(task), self._semaphore:
            record = await to_thread.run_sync(self.store.task_run, task.task_id)
            if record.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                return None  # Another worker finished it while we waited for its lease.
            task = await to_thread.run_sync(self.store.task_request, task.task_id)
            try:
                if task.action is TaskAction.IGNORED:
                    return await self._complete_ignored_task(task)
                if task.action is TaskAction.CLEANUP:
                    return await self._cleanup_context(task)
                return await self._run_context_task(task, record)
            except asyncio.CancelledError:
                await to_thread.run_sync(
                    self.store.mark_task_interrupted, task.task_id, "Service stopped; awaiting recovery"
                )
                raise
            except Exception as exc:
                await to_thread.run_sync(self.store.mark_task_failed, task.task_id, f"{exc}\n{traceback.format_exc()}")
                raise

    async def _run_context_task(self, task: AgentTask, record: TaskRunSummary) -> TaskRunResult:
        started_at = time.monotonic()
        existing = await to_thread.run_sync(self.store.get_context, task.context_key)
        resuming = record.thread_id is not None
        backend_name = record.backend if resuming else self.config.runtime.backend
        if existing and existing.backend != backend_name:
            existing = replace_context(existing, backend=backend_name, thread_id=None, revision=None)
        await to_thread.run_sync(self.store.mark_task_running, task.task_id, None, backend_name)
        if not resuming:
            task = await self._prepare_task(task, existing)
            await to_thread.run_sync(self.store.update_task_input, task)
        if task.action is TaskAction.IGNORED:
            run_result = TaskRunResult(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                backend=backend_name,
                thread_id=existing.thread_id if existing else None,
                turn_id=None,
                final_message=task.prompt,
            )
            await to_thread.run_sync(self.store.mark_task_done, run_result)
            return run_result
        if resuming:
            if existing is None or existing.session_worktree is None or not existing.session_worktree.is_dir():
                raise RuntimeError("Cannot resume task: its session workspace is unavailable")
            context = replace_context(existing, thread_id=record.thread_id)
        else:
            context = await to_thread.run_sync(self.worktrees.prepare_context, task, existing)
        context = replace_context(context, backend=backend_name)
        if context.session_worktree is None:
            context = replace_context(context, session_worktree=Path.cwd())
        event_worktree = record.event_worktree if resuming else None
        if task.workspace_policy == "event_snapshot":
            if not resuming:
                event_worktree = await to_thread.run_sync(self.worktrees.prepare_event_snapshot, task)
            elif event_worktree is not None and not event_worktree.is_dir():
                raise RuntimeError("Cannot resume task: its event workspace is unavailable")
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
        if resuming:
            prompt = (
                "The service interrupted this task. Continue from the saved conversation and existing workspace. "
                "Check which actions have already completed before repeating any operation. "
                "If the task is already complete, report its result.\n\nTask request:\n" + prompt
            )

        async def on_started(thread_id: str, turn_id: str | None) -> None:
            await to_thread.run_sync(
                functools.partial(
                    self.store.bind_task_execution, task.task_id, thread_id, turn_id, backend_name, context=context
                )
            )

        result = await self.backends.get(backend_name).execution.run_turn(
            cwd=context.session_worktree or Path.cwd(),
            prompt=prompt,
            developer_instructions=self._runtime_instructions(task),
            thread_id=context.thread_id,
            on_started=on_started,
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
            backend=backend_name,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            final_message=result.final_message,
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

    @contextlib.asynccontextmanager
    async def _context_execution(self, task: AgentTask):
        async with self._lock_for_context(task.context_key):
            await self._acquire_context_lease(task)
            heartbeat = asyncio.create_task(self._heartbeat_context_lease(task))
            try:
                yield
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
        logger.info("task cleanup started task_id={} context={}", task.task_id, task.context_key)
        await to_thread.run_sync(self.store.mark_task_running, task.task_id, None)
        context = await to_thread.run_sync(self.store.get_context, task.context_key)
        if context is not None:
            if context.thread_id:
                await self.backends.get(context.backend).execution.cleanup_thread(context.thread_id)
            await to_thread.run_sync(self.worktrees.remove_worktree, task.workspace, context.session_worktree)
            await to_thread.run_sync(self.store.delete_context, task.context_key)
        result = TaskRunResult(
            task_id=task.task_id,
            status=TaskStatus.COMPLETED,
            backend=context.backend if context else self.config.runtime.backend,
            thread_id=context.thread_id if context else None,
            turn_id=None,
            final_message="",
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

    async def _run_post_process_hooks(self, task: AgentTask, result: TaskRunResult) -> None:
        plugin_id = task.metadata.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            return
        for hook in self._post_process_hooks.get(plugin_id, []):
            await hook(task, result)

    async def _prepare_task(self, task: AgentTask, context: AgentContext | None) -> AgentTask:
        preparer = self._preparer_for(task)
        if preparer is None:
            return task
        records = await to_thread.run_sync(self.store.coalesced_tasks_for, task.task_id)
        coalesced = tuple(AgentTask.model_validate(record.task) for record in records)
        prepared = await preparer(task, coalesced, context)
        return prepared.model_copy(
            update={"metadata": {**prepared.metadata, "coalesced_task_ids": [item.task_id for item in coalesced]}}
        )

    def _preparer_for(self, task: AgentTask) -> TaskPreparer | None:
        plugin_id = task.metadata.get("plugin_id")
        preparer = self._task_preparers.get(plugin_id) if isinstance(plugin_id, str) else None
        if task.coalesce_key and preparer is None:
            raise ValueError("coalescing requires a registered plugin task preparer")
        return preparer

    def _runtime_prompt(
        self,
        task: AgentTask,
        *,
        event_worktree: Path | None,
        session_worktree: Path | None,
    ) -> str:
        return task.prompt.replace(
            "{{NYANPASU_EVENT_WORKTREE}}",
            str(event_worktree or session_worktree or Path.cwd()),
        ).replace(
            "{{NYANPASU_WORKTREE}}",
            str(event_worktree or session_worktree or Path.cwd()),
        )

    def _runtime_instructions(self, task: AgentTask) -> str:
        instructions = task.developer_instructions.strip()
        if task.instruction_docs:
            instructions += "\n\nConfigured instruction documents:\n"
            for doc in task.instruction_docs:
                instructions += f"\n--- {doc.name}"
                if doc.source:
                    instructions += f" ({doc.source})"
                instructions += f" ---\n{doc.content.strip()}\n"
        return instructions.strip()

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
        released = await to_thread.run_sync(self.store.release_context_leases_for_owner, self._owner_id)
        if released:
            logger.info("agent shutdown released context leases owner={} count={}", self._owner_id, released)
        await self.backends.close()
        logger.info("agent shutdown finished")
