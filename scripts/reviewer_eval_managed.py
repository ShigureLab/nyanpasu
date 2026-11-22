"""Replay pinned local review and children without GitHub or publication hooks."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from nyanpasu.agent import AgentService
from nyanpasu.config import ensure_state_dirs, load_config
from nyanpasu.models import AgentTask
from nyanpasu.web import WebPluginRuntime
from nyanpasu_github_reviewer import prompt, reference
from nyanpasu_github_reviewer.models import GitHubReviewerConfig
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin


async def run(path: Path) -> None:
    task = AgentTask.model_validate_json(path.read_bytes())
    directory = Path(task.metadata["evaluation_instructions"])
    prompt.INSTRUCTIONS_DIR = reference.INSTRUCTIONS = directory
    config = load_config()
    ensure_state_dirs(config)
    agent = AgentService(config)
    plugin = GitHubReviewerPlugin()
    plugin.config = GitHubReviewerConfig(dry_run=True)
    plugin.runtime = WebPluginRuntime(config=config, app=None, agent=agent)
    plugin.state_store = agent.store

    async def prepare_child(parent, request):
        if request.purpose in {"ci-analysis", "pr-compare", "pr-split"}:
            raise ValueError("remote PR/CI analysis is unavailable in this local evaluation")
        return await plugin.prepare_subtask(parent, request)

    agent.add_subtask_preparer(plugin.id, prepare_child)
    agent.add_task_control_tools(
        plugin.id, tuple(t for t in plugin.task_control_tools() if t.name == "review-inventory")
    )
    try:
        await agent.run_now(task)
        while True:
            result = agent.store.task_run(task.task_id)
            if result.status in {"completed", "failed", "cancelled"}:
                print(json.dumps({"task_id": task.task_id, "status": result.status}), flush=True)
                break
            await asyncio.sleep(0.25)
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1])))
