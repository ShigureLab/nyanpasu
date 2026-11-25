from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Annotated

import anyio
import typer
from loguru import logger
from nyanpasu_github.models import github_integration_from_config

from nyanpasu.agent import AgentService
from nyanpasu.config import ensure_state_dirs, load_config
from nyanpasu.web import WebPluginRuntime
from nyanpasu_github_reviewer.models import GitHubReviewerConfig
from nyanpasu_github_reviewer.plugin import GitHubPollAgent, GitHubReviewerPlugin, manual_review_event
from nyanpasu_github_reviewer.poller import GitHubEventsPoller
from nyanpasu_github_reviewer.store import GitHubReviewerStore

if TYPE_CHECKING:
    from pathlib import Path

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
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
def review(
    repo: Annotated[str, typer.Argument(help="Repository in OWNER/REPO form.")],
    pr: Annotated[int, typer.Argument(help="Pull request number.")],
) -> None:
    configure_logging()
    core_config = load_config()
    ensure_state_dirs(core_config)
    plugin_config = _plugin_config(
        core_config.plugins.settings.get("github_reviewer", {}), core_config.integrations, cwd=core_config.state_dir
    )

    async def run() -> None:
        agent = AgentService(core_config)
        plugin = _reviewer(plugin_config, agent)
        try:
            result = await plugin.dispatch_event(manual_review_event(repo, pr), wait_for_review=True)
            if result.get("reason"):
                typer.echo(result["reason"])
            if result.get("deferred"):
                raise typer.Exit(1)
        finally:
            await agent.shutdown()

    anyio.run(run)


@app.command()
def poll(
    once: Annotated[bool, typer.Option("--once", help="Run one poll cycle and exit.")] = False,
    rebaseline: Annotated[
        bool, typer.Option("--rebaseline", help="Refresh repo events cursor without reviews.")
    ] = False,
    interval: Annotated[int | None, typer.Option("--interval", help="Override poll interval seconds.")] = None,
    repo: Annotated[
        list[str] | None,
        typer.Option("--repo", "-r", help="Only poll this configured repository. Can be repeated."),
    ] = None,
) -> None:
    configure_logging()
    core_config = load_config()
    ensure_state_dirs(core_config)
    plugin_config = _plugin_config(
        core_config.plugins.settings.get("github_reviewer", {}), core_config.integrations, cwd=core_config.state_dir
    )
    repos = tuple(repo or plugin_config.repos)
    unknown = sorted(set(repos) - set(plugin_config.repos))
    if unknown:
        raise typer.BadParameter(f"repository is not configured: {', '.join(unknown)}")

    async def run() -> None:
        agent = AgentService(core_config)
        plugin = _reviewer(plugin_config, agent)
        poller = GitHubEventsPoller(
            plugin_config,
            store=GitHubReviewerStore(core_config.db_path),
            agent=GitHubPollAgent(plugin),
            event_status=agent.store.task_status,
        )
        try:
            if once or rebaseline:
                result = await poller.run_once(
                    repos,
                    wait_for_reviews=not rebaseline,
                    force_baseline=rebaseline,
                )
                typer.echo(
                    json.dumps(
                        {
                            "repos": result.repos,
                            "submitted": result.submitted,
                            "duplicates": result.duplicates,
                            "ignored": result.ignored,
                            "baselined": result.baselined,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                await poller.run_forever(repos, interval)
        finally:
            await agent.shutdown()

    anyio.run(run)


def _plugin_config(
    raw: dict[str, object], integrations: dict[str, dict[str, object]], *, cwd: Path
) -> GitHubReviewerConfig:
    github = github_integration_from_config(integrations.get("github"), cwd=cwd)
    return GitHubReviewerConfig.model_validate(raw).model_copy(update={"gh_env": github.gh_env()})


def _reviewer(config: GitHubReviewerConfig, agent: AgentService) -> GitHubReviewerPlugin:
    plugin = GitHubReviewerPlugin(config)
    plugin.bind_runtime(WebPluginRuntime(config=agent.config, app=None, agent=agent))
    return plugin


if __name__ == "__main__":
    app()
