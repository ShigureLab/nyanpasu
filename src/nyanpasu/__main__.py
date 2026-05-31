from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Annotated, Any

import anyio
import typer
import uvicorn
from loguru import logger

from nyanpasu.agent import AgentService
from nyanpasu.config import ensure_state_dirs, load_config
from nyanpasu.models import AgentTask
from nyanpasu.store import StateStore
from nyanpasu.web import create_app

if TYPE_CHECKING:
    from pathlib import Path

app = typer.Typer(no_args_is_help=True)
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS Z}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def configure_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=LOG_FORMAT, backtrace=False, diagnose=False)


@app.command()
def serve() -> None:
    configure_logging()
    resolved = load_config()
    ensure_state_dirs(resolved)
    uvicorn.run(create_app(resolved), host=resolved.server.host, port=resolved.server.port, log_level="info")


@app.command()
def run_task(path: Annotated[Path, typer.Argument(help="Path to a JSON task file.")]) -> None:
    configure_logging()
    resolved = load_config()
    ensure_state_dirs(resolved)
    task = _task_from_json(json.loads(path.read_text(encoding="utf-8")))

    async def run() -> None:
        agent = AgentService(resolved)
        try:
            result = await agent.run_now(task)
            typer.echo(
                json.dumps(
                    {
                        "task_id": result.task_id,
                        "status": result.status.value,
                        "thread_id": result.thread_id,
                        "turn_id": result.turn_id,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await agent.shutdown()

    anyio.run(run)


@app.command()
def status(limit: int = 20) -> None:
    resolved = load_config()
    store = StateStore(resolved.db_path)
    typer.echo(
        json.dumps(
            {
                "contexts": [
                    {
                        "context_key": context.context_key,
                        "thread_id": context.thread_id,
                        "session_worktree": str(context.session_worktree) if context.session_worktree else None,
                        "workspace_key": context.workspace_key,
                        "revision": context.revision,
                    }
                    for context in store.list_contexts()
                ],
                "tasks": [task.model_dump(mode="json") for task in store.recent_tasks(limit)],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def _task_from_json(data: dict[str, Any]) -> AgentTask:
    return AgentTask.model_validate(data)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
