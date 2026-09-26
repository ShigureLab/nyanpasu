from __future__ import annotations

import asyncio
import contextlib
import functools
import json
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
from nyanpasu.models import (
    AgentContext,
    AgentTask,
    SubtaskRequest,
    TaskAction,
    TaskRunResult,
    TaskRunSummary,
    TaskStatus,
)
from nyanpasu.store import StateStore, replace_context
from nyanpasu.task_control import TaskControl

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
        self.control = TaskControl(self)
        self._semaphore = asyncio.Semaphore(config.runtime.concurrency)
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._admitted_roots: set[str] = set()
        self._submit_lock = asyncio.Lock()
        self._post_process_hooks: dict[str, list[PostProcessHook]] = {}
        self._task_preparers: dict[str, TaskPreparer] = {}
        self._owner_id = f"{os.uname().nodename}:{os.getpid()}:{id(self)}"

    async def startup(self) -> None:
        async with self._submit_lock:
            for task in await to_thread.run_sync(self.store.unfinished_tasks):
                if task.spawned_by_task_id is not None:
                    continue  # Its admitted root restores the whole execution tree.
                if task.action is not TaskAction.CLEANUP and not await to_thread.run_sync(
                    self.store.task_is_active, task.task_id
                ):
                    continue
                plugin_id = task.metadata.get("plugin_id")
                if plugin_id and plugin_id not in (self.config.enabled_plugins or self.config.plugins):
                    logger.warning(
                        "task recovery deferred: plugin disabled task_id={} plugin={}", task.task_id, plugin_id
                    )
                    continue
                self._preparer_for(task)
                await to_thread.run_sync(
                    self.store.update_pending_task_backend, task.task_id, self.config.runtime.backend
                )
                self._schedule(task)
                logger.info("task recovery scheduled task_id={} context={}", task.task_id, task.context_key)

        # A failed or interrupted cleanup remains closing and must be retried.
        closing = await to_thread.run_sync(self.store.closing_contexts)
        keys = {scope.context_key for scope in closing}
        for scope in closing:
            if scope.parent_context_key in keys:
                continue
            records = await to_thread.run_sync(self.store.tasks_for_scope, scope.context_key, scope.generation)
            if any(record.action is TaskAction.CLEANUP and record.task_id in self._runners for record in records):
                continue
            source = await to_thread.run_sync(self.store.task_request, records[0].task_id)
            await self.submit(
                source.model_copy(
                    update={
                        "task_id": f"cleanup:{scope.context_key}:{time.time_ns()}",
                        "action": TaskAction.CLEANUP,
                        "prompt": "",
                        "dedupe_key": None,
                        "coalesce_key": None,
                        "spawned_by_task_id": None,
                    }
                )
            )

    def _schedule(self, task: AgentTask) -> None:
        if any(runner.get_name() == task.task_id for runner in self._tasks):
            return
        runner = asyncio.create_task(self._run_task_guarded(task), name=task.task_id)
        self._tasks.add(runner)
        self._runners[task.task_id] = runner
        runner.add_done_callback(self._tasks.discard)
        runner.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        runner.add_done_callback(lambda _: self._runners.pop(task.task_id, None))

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
            if task.action is TaskAction.CLEANUP:
                task = await to_thread.run_sync(self.store.task_request, task.task_id)
                await to_thread.run_sync(self.store.begin_context_cleanup, task.context_key, task.context_generation)
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
            raise

    async def _run_task(self, task: AgentTask) -> TaskRunResult | None:
        if task.action is TaskAction.CLEANUP:
            task = await to_thread.run_sync(self.store.task_request, task.task_id)
            try:
                await self._stop_context_tree(task)
                async with self._context_execution(task):
                    return await self._cleanup_context(task)
            except asyncio.CancelledError:
                await to_thread.run_sync(self.store.mark_task_interrupted, task.task_id, "Cleanup interrupted")
                raise
            except Exception as exc:
                await to_thread.run_sync(self.store.mark_task_failed, task.task_id, str(exc))
                raise
        if task.spawned_by_task_id is not None:
            root = await to_thread.run_sync(self.store.root_task_id, task.task_id)
            if root not in self._admitted_roots:
                raise RuntimeError("subtask has no admitted root execution")
            async with self._context_execution(task):
                return await self._run_execution(task)
        # Waiting for a busy context must not reserve capacity needed by other contexts.
        async with self._context_execution(task), self._semaphore:
            self._admitted_roots.add(task.task_id)
            try:
                for child in await to_thread.run_sync(self.store.subtasks, task.task_id):
                    if await to_thread.run_sync(self.store.task_is_active, child.task_id):
                        self._schedule(await to_thread.run_sync(self.store.task_request, child.task_id))
                return await self._run_execution(task)
            finally:
                await self._stop_descendants(task.task_id)
                self._admitted_roots.discard(task.task_id)

    async def _run_execution(self, task: AgentTask) -> TaskRunResult | None:
        try:
            while await to_thread.run_sync(self.store.task_is_active, task.task_id):
                record = await to_thread.run_sync(self.store.task_run, task.task_id)
                if await to_thread.run_sync(self.store.waiting_for, task.task_id):
                    await to_thread.run_sync(self.store.mark_task_waiting, task.task_id)
                    while not await to_thread.run_sync(self.store.wait_is_ready, task.task_id):
                        if not await to_thread.run_sync(self.store.task_is_active, task.task_id):
                            return None
                        await asyncio.sleep(0.05)
                    await to_thread.run_sync(self.store.resume_subtasks, task.task_id)
                    record = await to_thread.run_sync(self.store.task_run, task.task_id)
                task = await to_thread.run_sync(self.store.task_request, task.task_id)
                result = (
                    await self._complete_ignored_task(task)
                    if task.action is TaskAction.IGNORED
                    else await self._run_context_task(task, record)
                )
                if result.status is not TaskStatus.WAITING:
                    return result
        except (asyncio.CancelledError, Exception) as exc:
            runner = asyncio.current_task()
            if isinstance(exc, asyncio.CancelledError) or (runner is not None and runner.cancelling()):
                # Failed stop confirmation must block cleanup without forfeiting recovery.
                if await to_thread.run_sync(self.store.task_is_active, task.task_id):
                    await to_thread.run_sync(
                        self.store.mark_task_interrupted, task.task_id, "Service stopped; awaiting recovery"
                    )
            else:
                await to_thread.run_sync(self.store.mark_task_failed, task.task_id, f"{exc}\n{traceback.format_exc()}")
                await self.cancel_subtasks(task.task_id)
            raise
        return None

    async def create_subtask(self, parent_id: str, request: SubtaskRequest) -> AgentTask:
        root = await to_thread.run_sync(self.store.root_task_id, parent_id)
        if root not in self._admitted_roots:
            raise ValueError("parent root is not executing on this service")
        parent = await to_thread.run_sync(self.store.task_request, parent_id)
        if parent.workspace is None:
            raise ValueError("subtasks require a configured repository workspace")
        child = await to_thread.run_sync(self.store.create_subtask, parent_id, request)
        if await to_thread.run_sync(self.store.task_is_active, child.task_id):
            self._schedule(child)
        return child

    async def wait_for_subtasks(self, parent_id: str, child_ids: list[str]) -> dict[str, Any]:
        await to_thread.run_sync(self.store.wait_for_subtasks, parent_id, child_ids)
        return {"waiting": not await to_thread.run_sync(self.store.wait_is_ready, parent_id), "task_ids": child_ids}

    async def _stop_descendants(self, task_id: str) -> None:
        ids = [child.task_id for child in await to_thread.run_sync(self.store.subtasks, task_id)]
        await self.stop_tasks(ids)

    async def stop_tasks(self, ids: list[str]) -> None:
        runners = [self._runners[identity] for identity in ids if identity in self._runners]
        for runner in runners:
            if not runner.cancelling():
                runner.cancel()
        if runners:
            outcomes = await asyncio.gather(*runners, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    raise outcome

    async def cancel_subtasks(self, task_id: str) -> None:
        for child in await to_thread.run_sync(self.store.subtasks, task_id):
            await to_thread.run_sync(self.store.cancel_task_tree, child.task_id)
        await self._stop_descendants(task_id)

    async def _stop_context_tree(self, task: AgentTask) -> None:
        scopes = await to_thread.run_sync(self.store.begin_context_cleanup, task.context_key, task.context_generation)
        ids = []
        for scope in scopes:
            for record in await to_thread.run_sync(self.store.tasks_for_scope, scope.context_key, scope.generation):
                if record.action is not TaskAction.CLEANUP and record.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING,
                }:
                    ids.extend(await to_thread.run_sync(self.store.cancel_task_tree, record.task_id))
        await self.stop_tasks(list(set(ids)))

    async def _run_context_task(self, task: AgentTask, record: TaskRunSummary) -> TaskRunResult:
        started_at = time.monotonic()
        existing = await to_thread.run_sync(self.store.get_context, task.context_key)
        recovering = record.thread_id is not None
        backend_name = self.config.runtime.backend
        resuming = recovering and record.backend == backend_name
        if existing and existing.backend != backend_name:
            existing = replace_context(existing, backend=backend_name, thread_id=None, revision=None)
        # Keep the old session's backend identity until the replacement session starts.
        await to_thread.run_sync(self.store.mark_task_running, task.task_id, None, None if recovering else backend_name)
        if not resuming:
            task = await self._prepare_task(task, existing)
            await to_thread.run_sync(self.store.update_task_input, task)
        if task.action is TaskAction.IGNORED:
            run_result = TaskRunResult(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                backend=record.backend if recovering else backend_name,
                thread_id=record.thread_id if recovering else existing.thread_id if existing else None,
                turn_id=None,
                final_message=task.prompt,
            )
            await to_thread.run_sync(self.store.mark_task_done, run_result)
            return run_result
        if recovering:
            if existing is None or existing.session_worktree is None or not existing.session_worktree.is_dir():
                raise RuntimeError("Cannot resume task: its session workspace is unavailable")
            context = replace_context(existing, thread_id=record.thread_id if resuming else None)
            event_worktree = record.event_worktree
            if event_worktree is not None and not event_worktree.is_dir():
                raise RuntimeError("Cannot resume task: its event workspace is unavailable")
        else:
            context, event_worktree = await self._prepare_workspace(task, existing)
        if not await to_thread.run_sync(self.store.task_is_active, task.task_id):
            raise asyncio.CancelledError
        context = replace_context(context, backend=backend_name)
        if context.session_worktree is None:
            context = replace_context(context, session_worktree=Path.cwd())
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
        if resumed := task.metadata.get("resumed_subtasks"):
            evidence = [
                {
                    "task": (await to_thread.run_sync(self.store.task_run, identity)).model_dump(mode="json"),
                    "result": await to_thread.run_sync(self.store.subtask_result, identity),
                }
                for identity in resumed
            ]
            prompt += "\nSubtask results (verify evidence before using):\n" + json.dumps(evidence, ensure_ascii=False)
        if recovering:
            continuation = (
                "Continue from the saved conversation and existing workspace. "
                if resuming
                else f"The backend changed from {record.backend} to {backend_name}; this is a new native session. "
                f"The previous session was {record.backend}:{record.thread_id}. "
                "The existing workspace is preserved and may contain unfinished work or an earlier revision. "
                "Reconcile it with the current task request before continuing. "
            )
            prompt = (
                "The service interrupted this task. "
                + continuation
                + "Check which actions have already completed before repeating any operation. "
                "If the task is already complete, report its result.\n\nTask request:\n" + prompt
            )

        async def on_started(thread_id: str, turn_id: str | None) -> None:
            await to_thread.run_sync(
                functools.partial(
                    self.store.bind_task_execution, task.task_id, thread_id, turn_id, backend_name, context=context
                )
            )
            if not await to_thread.run_sync(self.store.task_is_active, task.task_id):
                raise asyncio.CancelledError

        async with self.control.turn(task.task_id) as control_prompt:
            result = await self.backends.get(backend_name).execution.run_turn(
                cwd=context.session_worktree or Path.cwd(),
                prompt=prompt,
                developer_instructions=self._runtime_instructions(task) + control_prompt,
                thread_id=context.thread_id,
                on_started=on_started,
            )
        if not await to_thread.run_sync(self.store.task_is_active, task.task_id):
            raise asyncio.CancelledError
        context = replace_context(
            context,
            thread_id=result.thread_id,
            revision=task.workspace.revision if task.workspace else context.revision,
        )
        await to_thread.run_sync(
            functools.partial(
                self.store.bind_task_execution,
                task.task_id,
                result.thread_id,
                result.turn_id,
                backend_name,
                context=context,
            )
        )
        pending = [
            child.task_id
            for child in await to_thread.run_sync(self.store.subtasks, task.task_id)
            if child.status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING}
        ]
        if pending and not await to_thread.run_sync(self.store.waiting_for, task.task_id):
            await to_thread.run_sync(self.store.wait_for_subtasks, task.task_id, pending)
        waiting = bool(await to_thread.run_sync(self.store.waiting_for, task.task_id))
        run_result = TaskRunResult(
            task_id=task.task_id,
            status=TaskStatus.WAITING if waiting else TaskStatus.COMPLETED,
            backend=backend_name,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            final_message=result.final_message,
            event_worktree=event_worktree,
            session_worktree=context.session_worktree,
        )
        await to_thread.run_sync(self.store.mark_task_done, run_result)
        if waiting:
            return run_result
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

    async def _prepare_workspace(
        self, task: AgentTask, existing: AgentContext | None
    ) -> tuple[AgentContext, Path | None]:
        async def prepare():
            context = await to_thread.run_sync(self.worktrees.prepare_context, task, existing)
            if existing is None:
                # Resource ownership must survive a backend failing before on_started.
                context = replace_context(context, backend=self.config.runtime.backend)
                await to_thread.run_sync(self.store.upsert_context, context)
            event_worktree = None
            if task.workspace_policy == "event_snapshot":
                event_worktree = await to_thread.run_sync(self.worktrees.prepare_event_snapshot, task)
                if event_worktree is not None:
                    await to_thread.run_sync(self.store.bind_task_event_worktree, task.task_id, event_worktree)
            return context, event_worktree

        preparation = asyncio.create_task(prepare())
        try:
            return await asyncio.shield(preparation)
        except asyncio.CancelledError:
            # Cancelling an await cannot stop the Git worker thread. Join it before
            # releasing the context lease, so cleanup never races workspace creation.
            await preparation
            raise

    @contextlib.asynccontextmanager
    async def _context_execution(self, task: AgentTask):
        async with self._lock_for_context(task.context_key):
            await self._acquire_context_lease(task)
            heartbeat = asyncio.create_task(self._heartbeat_context_lease(task, asyncio.current_task()))
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
        scopes = await to_thread.run_sync(self.store.begin_context_cleanup, task.context_key, task.context_generation)
        for scope in reversed(scopes):
            if scope.context_key == task.context_key:
                await self._cleanup_scope(scope.context_key, scope.generation, task.workspace)
            else:
                child_cleanup = task.model_copy(update={"context_key": scope.context_key})
                async with self._context_execution(child_cleanup):
                    records = await to_thread.run_sync(self.store.tasks_for_scope, scope.context_key, scope.generation)
                    source = await to_thread.run_sync(self.store.task_request, records[0].task_id)
                    await self._cleanup_scope(scope.context_key, scope.generation, source.workspace)
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

    async def _cleanup_scope(self, key: str, generation: int, workspace) -> None:
        context = await to_thread.run_sync(self.store.get_context, key)
        if context is not None:
            if context.thread_id:
                await self.backends.get(context.backend).execution.cleanup_thread(context.thread_id)
            await to_thread.run_sync(self.worktrees.remove_worktree, workspace, context.session_worktree)
        for record in await to_thread.run_sync(self.store.tasks_for_scope, key, generation):
            if record.event_worktree:
                await to_thread.run_sync(self.worktrees.remove_worktree, workspace, record.event_worktree)
        await to_thread.run_sync(self.store.close_context_scope, key, generation)

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

    async def _heartbeat_context_lease(self, task: AgentTask, runner: asyncio.Task | None = None) -> None:
        interval = self.config.runtime.context_lease_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            if task.action is not TaskAction.CLEANUP and not await to_thread.run_sync(
                self.store.task_is_active, task.task_id
            ):
                if runner is not None and not runner.cancelling():
                    runner.cancel()
                return
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
                if runner is not None and not runner.cancelling():
                    runner.cancel()
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
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        released = await to_thread.run_sync(self.store.release_context_leases_for_owner, self._owner_id)
        if released:
            logger.info("agent shutdown released context leases owner={} count={}", self._owner_id, released)
        await self.control.close()
        await self.backends.close()
        logger.info("agent shutdown finished")
