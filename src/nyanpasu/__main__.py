from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import anyio
import typer
import uvicorn
from loguru import logger

from nyanpasu.agent import AgentService
from nyanpasu.config import ensure_state_dirs, load_config
from nyanpasu.migration import migrate_state as migrate_state_file
from nyanpasu.models import AgentTask
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride
from nyanpasu.task_control import call_control
from nyanpasu.web import create_app

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
PathArgument = Path
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
def run_task(
    path: Annotated[PathArgument, typer.Argument(help="Path to a JSON task file.")],
    backend: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    target: str | None = None,
) -> None:
    configure_logging()
    resolved = load_config()
    ensure_state_dirs(resolved)
    task = _task_from_json(json.loads(path.read_text(encoding="utf-8")))
    if target is not None:
        task = task.model_copy(update={"execution_override": ExecutionOverride.model_validate(target)})
    overrides = {
        key: value
        for key, value in {"backend": backend, "model": model, "reasoning": reasoning}.items()
        if value is not None
    }
    if overrides:
        task = task.model_copy(update={"execution_override": task.execution_override.model_copy(update=overrides)})

    async def run() -> None:
        agent = AgentService(resolved)
        try:
            result = await agent.run_now(task)
            typer.echo(
                json.dumps(
                    {
                        "task_id": result.task_id,
                        "status": result.status.value,
                        "backend": result.backend,
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
                "contexts": [context.model_dump(mode="json") for context in store.list_contexts()],
                "tasks": [task.model_dump(mode="json") for task in store.recent_tasks(limit)],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@app.command()
def explain_target(
    kind: str = "default", backend: str | None = None, model: str | None = None, reasoning: str | None = None
) -> None:
    """Show the configured execution target and the source of each field."""
    target = load_config().resolve_execution(kind, ExecutionOverride(backend=backend, model=model, reasoning=reasoning))
    typer.echo(target.model_dump_json(indent=2))


@app.command()
def migrate_state(
    path: Annotated[PathArgument, typer.Argument(help="Backed-up, idle SQLite state file.")],
    native_home: Annotated[
        list[str], typer.Option(help="Historical backend=/absolute/native/history/directory; repeat per backend.")
    ],
    isolated_home: Annotated[
        list[str] | None, typer.Option(help="Optional backend=/absolute/reader/root; defaults to its native directory.")
    ] = None,
) -> None:
    """Migrate pre-hybrid state explicitly. Stop the service and retain a backup first."""

    def locations(values: list[str]) -> dict[str, Path]:
        result = {}
        for value in values:
            backend, separator, directory = value.partition("=")
            if not separator or not backend or not Path(directory).is_absolute() or backend in result:
                raise ValueError("home mappings require one backend=/absolute/directory entry per backend")
            result[backend] = Path(directory)
        return result

    try:
        typer.echo(
            json.dumps(
                migrate_state_file(
                    path, native_homes=locations(native_home), isolated_homes=locations(isolated_home or [])
                )
            )
        )
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def subtask(control: Path, request: Path) -> None:
    """Send a JSON request using the current turn's control file."""
    try:
        typer.echo(json.dumps(call_control(control, json.loads(request.read_text())), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def _task_from_json(data: dict[str, Any]) -> AgentTask:
    return AgentTask.model_validate(data)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
