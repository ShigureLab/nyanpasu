from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from nyanpasu.config import CodexConfig, NyanpasuConfig
from nyanpasu.models import AgentContext, TaskAction
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, RepoSettings
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin, manual_event_task

if TYPE_CHECKING:
    from pathlib import Path


def _plugin(tmp_path: Path) -> GitHubReviewerPlugin:
    plugin = GitHubReviewerPlugin(
        GitHubReviewerConfig(
            repos={"ExampleOrg/ExampleRepo": RepoSettings(local_path=tmp_path / "repo", base_branches=("main",))},
            github_login="review-bot",
            dry_run=True,
        )
    )
    plugin.runtime = Mock(
        config=NyanpasuConfig(state_dir=tmp_path, codex=CodexConfig(model="runtime-model", reasoning_effort="high"))
    )
    return plugin


def _pr_payload(action: str, sha: str = "head-a") -> dict[str, object]:
    return {
        "action": action,
        "repository": {"full_name": "ExampleOrg/ExampleRepo"},
        "pull_request": {
            "number": 1,
            "html_url": "https://github.com/ExampleOrg/ExampleRepo/pull/1",
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": sha},
        },
    }


def _task(plugin: GitHubReviewerPlugin, action: str, sha: str):
    return plugin.event_to_task(
        parse_github_event("pull_request", f"{action}-{sha}", _pr_payload(action, sha), agent_login="review-bot")
    )


def _live_pr(**updates):
    return {
        "number": 1,
        "url": "https://github.com/ExampleOrg/ExampleRepo/pull/1",
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefName": "feature",
        "headRefOid": "head-b",
        **updates,
    }


def _stub_github(monkeypatch, **updates):
    module = importlib.import_module("nyanpasu_github_reviewer.plugin")
    monkeypatch.setattr(module, "gh_json", lambda *args, **kwargs: _live_pr(**updates))


def test_event_conversion_defers_session_and_target_decisions(tmp_path: Path) -> None:
    task = _task(_plugin(tmp_path), "opened", "head-a")

    assert task.coalesce_key
    assert task.developer_instructions == ""
    assert "head-a" not in task.prompt
    assert "review_mode" not in task.metadata
    assert "previous_head_sha" not in task.metadata
    assert task.metadata["triggers"]


@pytest.mark.anyio
@pytest.mark.parametrize("reverse", [False, True])
async def test_preparation_uses_current_pr_head_for_merged_events(tmp_path: Path, monkeypatch, reverse: bool) -> None:
    plugin = _plugin(tmp_path)
    opened = _task(plugin, "opened", "head-a")
    updated = _task(plugin, "synchronize", "head-b")
    first, second = (updated, opened) if reverse else (opened, updated)
    _stub_github(monkeypatch)

    prepared = await plugin.prepare_task(first, (second,), None)

    assert prepared.task_id == first.task_id
    assert prepared.workspace is not None and prepared.workspace.revision == "head-b"
    assert prepared.metadata["pull_request"]["head_sha"] == "head-b"
    assert len(prepared.metadata["triggers"]) == 2
    assert prepared.prompt.startswith("Review ")
    assert prepared.prompt.count("Target head:") == 1
    assert "Target head: head-b" in prepared.prompt
    assert "head-a" not in prepared.prompt
    assert "Additional coalesced task context" not in prepared.prompt
    assert "Powered by Nyanpasu with runtime-model high" in prepared.prompt


@pytest.mark.anyio
async def test_preparation_reads_completed_context_at_execution(tmp_path: Path, monkeypatch) -> None:
    plugin = _plugin(tmp_path)
    queued = _task(plugin, "synchronize", "head-b")
    _stub_github(monkeypatch)
    context = AgentContext(
        context_key=queued.context_key,
        thread_id="existing-thread",
        session_worktree=tmp_path / "worktree",
        workspace_key="ExampleOrg/ExampleRepo",
        revision="head-a",
    )

    prepared = await plugin.prepare_task(queued, (), context)

    assert prepared.prompt.startswith("Continue reviewing ")
    assert "Previous task head (not proof of completed review): head-a" in prepared.prompt


@pytest.mark.anyio
@pytest.mark.parametrize("updates", [{"state": "CLOSED"}, {"isDraft": True}, {"baseRefName": "other"}])
async def test_no_review_when_pr_becomes_ineligible_in_queue(tmp_path: Path, monkeypatch, updates) -> None:
    plugin = _plugin(tmp_path)
    queued = _task(plugin, "opened", "head-a")
    _stub_github(monkeypatch, **updates)

    prepared = await plugin.prepare_task(queued, (), None)

    assert prepared.action is TaskAction.IGNORED
    assert "Review skipped" in prepared.prompt


def test_manual_review_preserves_explicit_request(tmp_path: Path, monkeypatch) -> None:
    _stub_github(monkeypatch)
    plugin = _plugin(tmp_path)
    assert plugin.config is not None
    task = manual_event_task(plugin.config, "ExampleOrg/ExampleRepo", 1)

    assert task.metadata["triggers"][0]["kind"] == "manual_review"
