from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from test_github_events import issue_comment_payload, pr_payload, pull_request_review_payload, review_comment_payload

from nyanpasu.config import CodexConfig, NyanpasuConfig
from nyanpasu.models import AgentContext, TaskAction
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, RepoSettings, ReviewTrigger
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


def _pr_payload(action: str, sha: str = "head-a") -> dict[str, Any]:
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
        "draft": False,
        "base": {"ref": "main", "sha": "base-a"},
        "head": {"ref": "feature", "sha": "head-b"},
        **updates,
    }


def _stub_github(monkeypatch, **updates):
    module = importlib.import_module("nyanpasu_github_reviewer.plugin")
    monkeypatch.setattr(module, "gh_json", lambda *args, **kwargs: _live_pr(**updates))
    monkeypatch.setattr(
        module,
        "build_inventory",
        lambda config, task: {
            "inventory_id": task.workspace.revision,
            "head_sha": task.workspace.revision,
            "base_ref": task.metadata["pull_request"]["base_ref"],
            "merge_base_sha": "base-a",
        },
    )


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
    assert (
        'Powered by <a href="https://github.com/ShigureLab/nyanpasu">Nyanpasu</a> with runtime-model high'
        in prepared.prompt
    )


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
@pytest.mark.parametrize("updates", [{"state": "CLOSED"}, {"draft": True}, {"base": {"ref": "other"}}])
async def test_no_review_when_pr_becomes_ineligible_in_queue(tmp_path: Path, monkeypatch, updates) -> None:
    plugin = _plugin(tmp_path)
    queued = _task(plugin, "opened", "head-a")
    _stub_github(monkeypatch, **updates)

    prepared = await plugin.prepare_task(queued, (), None)

    assert prepared.action is TaskAction.IGNORED


def test_manual_review_preserves_explicit_request(tmp_path: Path, monkeypatch) -> None:
    _stub_github(monkeypatch)
    plugin = _plugin(tmp_path)
    assert plugin.config is not None
    task = manual_event_task(plugin.config, "ExampleOrg/ExampleRepo", 1)

    assert task.metadata["triggers"][0]["kind"] == "manual_review"


@pytest.mark.parametrize("github_event", ["issue_comment", "pull_request_review_comment", "pull_request_review"])
@pytest.mark.parametrize("body", ["/review", "context " * 200 + "\n/review"])
def test_review_command_becomes_runnable_explicit_request(tmp_path: Path, monkeypatch, github_event: str, body: str):
    _stub_github(monkeypatch)
    if github_event == "issue_comment":
        payload = issue_comment_payload(body)
    elif github_event == "pull_request_review_comment":
        payload = review_comment_payload(body=body, in_reply_to_id=None)
    else:
        payload = pull_request_review_payload(body)
    event = parse_github_event(github_event, "command-1", payload, agent_login="review-bot")

    task = _plugin(tmp_path).event_to_task(event)

    assert task.action is TaskAction.RUN
    assert ReviewTrigger.model_validate(task.metadata["triggers"][0]).explicit_request
    assert task.metadata["triggers"][0]["kind"] == "review_command"


@pytest.mark.parametrize(
    ("base", "trunk", "expected"),
    [
        ("lower", "main", TaskAction.RUN),
        ("lower", "release", TaskAction.IGNORED),
        ("main", "release", TaskAction.IGNORED),
    ],
)
def test_stack_eligibility_uses_trunk(tmp_path, base, trunk, expected):
    payload = _pr_payload("opened")
    payload["pull_request"]["base"] = {"ref": base, "sha": "lower-head"}
    payload["pull_request"]["stack"] = {"number": 7, "base": {"ref": trunk}}
    task = _plugin(tmp_path).event_to_task(parse_github_event("pull_request", "stack-open", payload))

    assert task.action is expected
    assert task.metadata["pull_request"]["base_ref"] == base
    assert task.metadata["pull_request"]["stack"] == {"number": 7, "base_ref": trunk}


@pytest.mark.anyio
async def test_hydration_and_preparation_refresh_stack_membership(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    payload = _pr_payload("opened")
    payload["pull_request"]["base"] = {"ref": "lower"}
    stack = {"number": 7, "base": {"ref": "main"}}
    _stub_github(monkeypatch, base={"ref": "lower", "sha": "lower-head"}, stack=stack)

    queued = plugin.event_to_task(parse_github_event("pull_request", "stack-open", payload))
    prepared = await plugin.prepare_task(queued, (), None)
    assert prepared.action is TaskAction.RUN
    assert prepared.metadata["pull_request"]["base_ref"] == "lower"
    assert "Stack #7; trunk: main" in prepared.prompt
    assert prepared.metadata["review_inventory"]["base_ref"] == "lower"

    # Leaving the stack while queued cannot inherit the old trunk's permission.
    _stub_github(monkeypatch, base={"ref": "lower", "sha": "lower-head"}, stack=None)
    assert (await plugin.prepare_task(queued, (), None)).action is TaskAction.IGNORED


def test_fetch_pr_reads_stack_and_base_from_rest(tmp_path, monkeypatch):
    module = importlib.import_module("nyanpasu_github_reviewer.plugin")
    calls = []

    def github(args, **kwargs):
        calls.append(args)
        return _live_pr(stack={"number": 7, "base": {"ref": "main"}})

    monkeypatch.setattr(module, "gh_json", github)
    config = _plugin(tmp_path).config
    assert config is not None
    pr = module._fetch_pr(config, "ExampleOrg/ExampleRepo", 1)
    assert calls == [["api", "repos/ExampleOrg/ExampleRepo/pulls/1"]]
    assert pr.base_sha == "base-a"
    assert pr.stack is not None and pr.stack.number == 7


@pytest.mark.anyio
@pytest.mark.parametrize(
    "updates",
    [
        {"state": "closed"},
        {"draft": True},
        {"stack": {"number": 7, "base": {"ref": "release"}}},
    ],
)
async def test_publication_verification_rechecks_eligibility(tmp_path, monkeypatch, updates):
    plugin = _plugin(tmp_path)
    task = _task(plugin, "opened", "head-a")
    task = task.model_copy(update={"metadata": {**task.metadata, "review_inventory": {"inventory_id": "id"}}})
    _stub_github(monkeypatch, **updates)
    with pytest.raises(ValueError, match="no longer eligible"):
        await plugin.scope_control(task, "review-verify", {})


@pytest.mark.parametrize(
    "github_event,payload,item_field",
    [
        ("issue_comment", issue_comment_payload("/review"), "comment"),
        ("pull_request_review_comment", review_comment_payload(body="/review"), "comment"),
        ("pull_request_review", pull_request_review_payload("/review"), "review"),
    ],
)
def test_distinct_comment_requests_remain_admissible(tmp_path, monkeypatch, github_event, payload, item_field):
    plugin = _plugin(tmp_path)
    _stub_github(monkeypatch)
    store = StateStore(tmp_path / "state.sqlite3")

    def submit(delivery, raw):
        event = parse_github_event(github_event, delivery, raw, agent_login="review-bot")
        return store.record_task(plugin.event_to_task(event))

    assert submit("original", payload)
    assert not submit("redelivery", payload)
    edited = payload | {"action": "edited", item_field: payload[item_field] | {"body": "/review again"}}
    assert submit("edited", edited)
    assert not submit("edited-redelivery", edited)
    another = payload | {item_field: payload[item_field] | {"id": 900}}
    assert submit("another-comment", another)
    if item_field == "comment":
        # A later edit may repeat the original body and still request a new turn.
        updated = payload | {
            "action": "edited",
            item_field: payload[item_field] | {"updated_at": "2026-05-30T10:10:00Z"},
        }
        assert submit("later-edit", updated)


def test_explicit_review_requests_on_same_head_remain_admissible(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    _stub_github(monkeypatch)
    store = StateStore(tmp_path / "state.sqlite3")
    payload = pr_payload("review_requested") | {"requested_reviewer": {"login": "review-bot"}}
    for delivery in ("request-1", "request-2"):
        event = parse_github_event("pull_request", delivery, payload, agent_login="review-bot")
        assert store.record_task(plugin.event_to_task(event))
        assert not store.record_task(plugin.event_to_task(event))
    assert plugin.config is not None
    for _ in range(2):
        assert store.record_task(manual_event_task(plugin.config, "ExampleOrg/ExampleRepo", 123))
