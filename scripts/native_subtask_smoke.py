"""Exercise managed subtasks with configured native drivers and isolated state."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from nyanpasu.agent import AgentService
from nyanpasu.config import ensure_state_dirs, load_config
from nyanpasu.models import AgentTask, TaskAction, TaskStatus
from nyanpasu.redaction import redact
from nyanpasu.targets import ExecutionOverride

DELAY_MARKER = ".restart-delay-started"
TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def save_report(output: Path, report: dict[str, Any]) -> None:
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def task_snapshot(service: AgentService, parent_id: str) -> dict[str, Any]:
    records = [service.store.task_run(parent_id), *service.store.subtasks(parent_id)]
    return {
        record.task_id: {
            "status": record.status.value,
            "thread_id": record.thread_id,
            "turn_id": record.turn_id,
            "turns": len(service.store.task_turns(record.task_id)),
        }
        for record in records
    }


def process_snapshot(processes: list[psutil.Process]) -> list[dict[str, Any]]:
    records = []
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
            records.append(
                {
                    **process.as_dict(attrs=["pid", "name", "ppid", "status", "create_time", "cwd"]),
                    "pgid": os.getpgid(process.pid),
                    "sid": os.getsid(process.pid),
                }
            )
    return records


async def wait_for_stopped(processes: list[psutil.Process], timeout: float = 2) -> list[psutil.Process]:
    """Observe exit without racing the native runtime's asyncio child reaper."""
    deadline = time.monotonic() + timeout
    while True:
        running = []
        for process in processes:
            with contextlib.suppress(psutil.NoSuchProcess):
                if process.is_running() and process.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                    running.append(process)
        if not running or time.monotonic() >= deadline:
            return running
        await asyncio.sleep(0.05)


async def stop_service(service: AgentService, *, observe_seconds: float = 0) -> dict[str, Any]:
    """Stop this experiment's owned processes; never inspect or stop the host service."""
    owned = psutil.Process().children(recursive=True)
    observed = process_snapshot(owned)
    error = None
    try:
        async with asyncio.timeout(60):
            await service.shutdown()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {redact(str(exc))}"
    owned = list({process.pid: process for process in [*owned, *psutil.Process().children(recursive=True)]}.values())
    remaining = await wait_for_stopped(owned)
    leaked = process_snapshot(remaining)
    observation_started = time.monotonic()
    if remaining and observe_seconds:
        remaining = await wait_for_stopped(remaining, timeout=observe_seconds)
    observation_elapsed = time.monotonic() - observation_started
    after_observation = process_snapshot(remaining)
    forced = [process.pid for process in remaining]
    for process in reversed(remaining):
        with contextlib.suppress(psutil.NoSuchProcess):
            process.terminate()
    if remaining:
        remaining = await wait_for_stopped(remaining)
    for process in reversed(remaining):
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()
    if remaining:
        remaining = await wait_for_stopped(remaining)
    return {
        "clean_shutdown": error is None and not leaked,
        "error": error,
        "observed_owned_pids": [process.pid for process in owned],
        "observed_owned_processes": observed,
        "running_after_shutdown_grace": leaked,
        "observation_seconds": observation_elapsed,
        "running_after_observation": after_observation,
        "forced_stop_pids": forced,
        "remaining_owned_pids": [process.pid for process in remaining],
    }


async def wait_for_restart_window(service: AgentService, parent_id: str) -> dict[str, Any]:
    while True:
        parent = service.store.task_run(parent_id)
        children = service.store.subtasks(parent_id)
        if parent.status in TERMINAL:
            raise AssertionError(f"Parent became {parent.status} before the restart window")
        delayed = []
        for child in children:
            context = service.store.get_context(child.context_key)
            if (
                child.status is TaskStatus.RUNNING
                and child.thread_id is not None
                and context is not None
                and context.session_worktree is not None
                and (context.session_worktree / DELAY_MARKER).is_file()
            ):
                delayed.append(child.task_id)
        if parent.status is TaskStatus.WAITING and parent.thread_id is not None and len(children) == 2 and delayed:
            return {
                "tasks": task_snapshot(service, parent_id),
                "child_ids": [child.task_id for child in children],
                "delayed_running_children": delayed,
            }
        await asyncio.sleep(0.05)


async def wait_for_completion(service: AgentService, parent_id: str) -> None:
    while True:
        parent = service.store.task_run(parent_id)
        if parent.status in TERMINAL:
            assert parent.status is TaskStatus.COMPLETED, f"Parent ended as {parent.status}: {redact(parent.error)}"
            return
        await asyncio.sleep(0.05)


async def run(
    output: Path, backend: str, *, restart_on_wait: bool = False, observe_shutdown_seconds: float = 0
) -> dict[str, Any]:
    """Exercise native delegation and parent continuation in a new, isolated state directory."""
    output.mkdir(parents=True, exist_ok=False)
    source = load_config()
    config = source.model_copy(
        update={
            "state_dir": output / "state",
            "memory": source.memory.model_copy(update={"enabled": False}),
            "runtime": source.runtime.model_copy(update={"concurrency": 1}),
        }
    )
    ensure_state_dirs(config)
    target = config.resolve_execution(override=ExecutionOverride(backend=backend))
    context_key = "native-subtask-smoke:" + uuid4().hex
    expected = {name: uuid4().hex for name in ("alpha", "beta")}
    requests = []
    delay_instruction = ""
    if restart_on_wait:
        delay_instruction = (
            "This experiment deliberately interrupts a native turn. Before writing nonce.txt, execute one bounded "
            "15-second Python sleep in your workspace: create .restart-delay-started before sleeping. "
            "If that marker already exists after recovery, skip the delay and continue. "
            "Do not repeat the delay, wait for another agent, or perform any unrelated operation. "
        )
    for name, nonce in expected.items():
        requests.append(
            {
                "action": "create",
                "input": {
                    "request_key": name,
                    "purpose": "runtime-smoke",
                    "memory_enabled": False,
                    "execution": {"backend": backend},
                    "prompt": delay_instruction
                    + f"Write the exact string {nonce!r} into nonce.txt in your current directory. "
                    + f"Call task-control complete with summary {name!r}, artifacts ['nonce.txt'], "
                    + f"data {{'name': {name!r}, 'nonce': {nonce!r}}}. Then end this turn. Do nothing else.",
                    "developer_instructions": (
                        "This is a native subtask acceptance check. Use the supplied task control for task lifecycle. "
                        "Do not delegate, browse, use memory, or write outside your current workspace."
                    ),
                },
            }
        )
    task = AgentTask(
        task_id=uuid4().hex,
        action=TaskAction.RUN,
        context_key=context_key,
        execution_override=ExecutionOverride(backend=backend),
        developer_instructions=(
            "Run this bounded runtime acceptance check using managed task control. "
            "Do not use native subagents. Do not browse, use memory, change repositories or publish anything. "
            "Only write in your workspace. Child results must arrive in a later service continuation before the final file."
        ),
        prompt="Create both children using these exact JSON requests through the supplied control client:\n"
        + json.dumps(requests)
        + "\nAfter creating them, call await with mode any and both returned task IDs, then end this turn. "
        "On service continuation, collect the delivered children's frozen data. If a child is still missing, "
        "call await mode all for just that child and end the turn again. Once both completed results have arrived, "
        "write parent-result.json containing an object mapping each returned data.name to data.nonce. "
        "Write it only after receiving both results, then finish. Do not poll, sleep, inspect results early, "
        "recreate children, or return a final conclusion before the continuation.",
    )
    service = AgentService(config)
    started = time.monotonic()
    report: dict[str, Any] = {
        "task_id": task.task_id,
        "context_key": context_key,
        "execution": target.model_dump(mode="json"),
        "restart_on_wait": restart_on_wait,
        "expected_result": expected,
        "status": "running",
        "stage": "submit",
    }
    before_restart = None
    save_report(output, report)
    try:
        async with asyncio.timeout(900):
            await service.submit(task)
            if restart_on_wait:
                before_restart = await wait_for_restart_window(service, task.task_id)
                report.update(
                    stage="stopping_for_restart",
                    restart_before=before_restart,
                    restart_at_seconds=time.monotonic() - started,
                )
                save_report(output, report)
                stopped = await stop_service(service, observe_seconds=observe_shutdown_seconds)
                report.update(restart_shutdown=stopped, restart_stopped=task_snapshot(service, task.task_id))
                save_report(output, report)
                assert stopped["clean_shutdown"] and not stopped["remaining_owned_pids"], stopped
                service = AgentService(config)
                report["stage"] = "recovering_from_sqlite"
                save_report(output, report)
                await service.startup()
            await wait_for_completion(service, task.task_id)
        report["task_elapsed_seconds"] = time.monotonic() - started
        result = service.store.task_run(task.task_id)
        context = service.store.get_context(context_key)
        assert context is not None and context.session_worktree is not None
        actual = json.loads((context.session_worktree / "parent-result.json").read_text())
        children = service.store.subtasks(task.task_id)
        assert len(children) == 2 and all(child.status is TaskStatus.COMPLETED for child in children)
        assert actual == expected, actual
        turns = service.store.task_turns(task.task_id)
        assert len(turns) >= 2, "parent did not continue in another native turn"
        with sqlite3.connect(config.db_path) as conn:
            receipts = conn.execute(
                "SELECT child_task_id,consumed_at FROM subtask_deliveries WHERE parent_task_id=?", (task.task_id,)
            ).fetchall()
        assert {identity for identity, _ in receipts} == {child.task_id for child in children}
        assert all(stamp is not None for _, stamp in receipts)
        assert service.store.pending_subtask_deliveries(task.task_id) == []
        assert service.store.unconsumed_subtasks(task.task_id) == []
        child_results = {child.task_id: service.store.subtask_result(child.task_id) for child in children}
        assert all(value is not None for value in child_results.values())
        assert {
            value["data"]["name"]: value["data"]["nonce"] for value in child_results.values() if value is not None
        } == expected
        for value in child_results.values():
            assert value is not None
            artifact = value["artifacts"][0]
            content = Path(artifact["path"]).read_bytes()
            assert content.decode() == expected[value["data"]["name"]]
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        after = task_snapshot(service, task.task_id)
        if before_restart is not None:
            assert {child.task_id for child in children} == set(before_restart["child_ids"]), "New child after restart"
            for identity, previous in before_restart["tasks"].items():
                if previous["thread_id"] is not None:
                    assert after[identity]["thread_id"] == previous["thread_id"], "Native session identity changed"
                    assert {turn["thread_id"] for turn in service.store.task_turns(identity)} == {
                        previous["thread_id"]
                    }, "Unexpected native session in task history"
            report["restart_after"] = after
            report["restart_identity_preserved"] = True
        report.update(
            stage="cleanup",
            parent_turns=len(turns),
            thread_id=result.thread_id,
            task_states=after,
            children=child_results,
            receipts=receipts,
            result=actual,
        )
        save_report(output, report)
        contexts = [context, *(service.store.get_context(child.context_key) for child in children)]
        workspaces = [
            item.session_worktree for item in contexts if item is not None and item.session_worktree is not None
        ]
        cleanup = await service.run_now(
            AgentTask(
                task_id=uuid4().hex,
                action=TaskAction.CLEANUP,
                context_key=context_key,
                prompt="Clean up the native acceptance task tree",
                execution_override=ExecutionOverride(backend=backend),
            )
        )
        assert cleanup.status is TaskStatus.COMPLETED
        assert all(
            service.store.context_scope(key).lifecycle == "closed"
            for key in [context_key, *(c.context_key for c in children)]
        )
        assert all(not workspace.exists() for workspace in workspaces)
        report.update(status="passed", stage="completed", cleanup="completed", removed_workspaces=len(workspaces))
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {redact(str(exc))}")
    finally:
        report["final_shutdown"] = await stop_service(service, observe_seconds=observe_shutdown_seconds)
        if not report["final_shutdown"]["clean_shutdown"] or report["final_shutdown"]["remaining_owned_pids"]:
            report.update(status="failed", shutdown_failure=True)
        report["elapsed_seconds"] = time.monotonic() - started
        report["retained_state"] = str(config.db_path)
        save_report(output, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for isolated state and retained evidence")
    parser.add_argument("--backend", default="codex", help="Configured native backend")
    parser.add_argument(
        "--restart-on-wait", action="store_true", help="Stop and recover the isolated service while children run"
    )
    parser.add_argument(
        "--observe-shutdown-seconds",
        type=float,
        default=0,
        help="Observe a failed shutdown before stopping remaining owned processes; does not turn failure into success",
    )
    args = parser.parse_args()
    result = asyncio.run(
        run(
            args.output.resolve(),
            args.backend,
            restart_on_wait=args.restart_on_wait,
            observe_shutdown_seconds=args.observe_shutdown_seconds,
        )
    )
    print(
        json.dumps(
            {
                key: result.get(key)
                for key in ("status", "task_id", "parent_turns", "cleanup", "elapsed_seconds", "error")
            }
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
