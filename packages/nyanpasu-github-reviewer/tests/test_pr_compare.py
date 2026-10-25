from __future__ import annotations

import importlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin
from nyanpasu_github_reviewer.scope import build_inventory, review_source
from tests.test_review_reference import repository


@pytest.fixture
def comparison_review(tmp_path, monkeypatch):
    repo, base, head, target = repository(tmp_path)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    git("checkout", "-b", "similar", base)
    (repo / "snapshot.py").write_text("refresh = False\n")
    git("add", ".")
    git("commit", "-m", "similar implementation")
    peer_head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/447/head", peer_head)
    git("checkout", "main")
    config = NyanpasuConfig.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "tasks": {"kinds": {"github_reviewer.pr-compare": {"execution": {"model": "compare-model"}}}},
        }
    )
    pr = PullRequestRef(
        repo="owner/repo",
        number=464,
        url="https://github.com/owner/repo/pull/464",
        base_ref="main",
        base_sha=target,
        head_ref="feature",
        head_sha=head,
        state="open",
        draft=False,
    )
    peer = pr.model_copy(
        update={
            "number": 447,
            "url": "https://github.com/owner/repo/pull/447",
            "head_ref": "similar",
            "head_sha": peer_head,
        }
    )
    fetch = Mock(return_value=peer)
    monkeypatch.setattr(importlib.import_module("nyanpasu_github_reviewer.plugin"), "_fetch_pr", fetch)
    plugin = GitHubReviewerPlugin(GitHubReviewerConfig())
    plugin.runtime = Mock(config=config)
    plugin.state_store = StateStore(config.db_path)
    parent = AgentTask(
        task_id="parent",
        context_key="github:owner/repo#464",
        kind="github_reviewer.review",
        execution=config.resolve_execution("github_reviewer.review"),
        action=TaskAction.RUN,
        prompt="Review SFT weight management",
        workspace=WorkspaceRef(key=pr.repo, local_path=repo, remote=str(repo), revision=head),
        metadata={"plugin_id": plugin.id, "pull_request": pr.model_dump(mode="json")},
    )
    parent = parent.model_copy(
        update={"metadata": {**parent.metadata, "review_inventory": build_inventory(config, parent)}}
    )
    plugin.state_store.record_task(parent)
    plugin.state_store.mark_task_running(parent.task_id, None)
    return plugin, parent, peer, fetch, git


async def create_comparison(plugin, parent):
    request = SubtaskRequest(
        request_key="compare:464:447",
        purpose="pr-compare",
        prompt="Compare the overlapping SFT snapshot behavior",
        inputs={"related_prs": [447]},
    )
    prepared = await plugin.prepare_subtask(parent, request)
    return plugin.state_store.create_subtask(
        parent.task_id,
        request,
        prepared=prepared,
        execution=plugin.runtime.config.resolve_execution(prepared.kind, prepared.execution),
    )


@pytest.mark.anyio
async def test_managed_comparison_can_read_both_pinned_implementations_after_peer_ref_moves(comparison_review):
    plugin, parent, peer, fetch, git = comparison_review
    child = await create_comparison(plugin, parent)
    fetch.assert_called_once_with(plugin.config, "owner/repo", 447)
    assert child.spawned_by_task_id == parent.task_id
    assert child.kind == "github_reviewer.pr-compare"
    assert child.execution.model == "compare-model"
    assert child.memory.read_domains == ()
    comparison = child.metadata["inputs"]["comparison"]
    assert comparison["source"] == review_source(parent.metadata["review_inventory"])
    assert comparison["related_prs"][0]["pull_request"]["head_sha"] == peer.head_sha
    assert json.loads(child.prompt.split("Pinned PR comparison (JSON data):\n")[1]) == comparison

    # The PR moves after dispatch. The child must still have the original comparison,
    # while freshness policy requires a new analysis before using changed evidence.
    git("update-ref", "refs/pull/447/head", parent.workspace.revision)
    context = WorktreeManager(plugin.runtime.config).prepare_context(child, None)
    assert context.session_worktree is not None
    original = subprocess.run(
        ["git", "show", f"{peer.head_sha}:snapshot.py"],
        cwd=context.session_worktree,
        capture_output=True,
        text=True,
        check=True,
    )
    assert original.stdout == "refresh = False\n"
    assert (context.session_worktree / "author-only.txt").read_text() == "implementation information"
    assert git("branch", "--show-current") == "main"
    plugin._validate_recovered_subtasks()
    assert plugin.state_store.task_status(child.task_id) == "queued"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["head_sha", "base_ref", "merge_base_sha", "inventory_id", "workspace"])
async def test_comparison_recovery_rejects_changed_parent_range(comparison_review, change):
    plugin, parent, _, _, _ = comparison_review
    child = await create_comparison(plugin, parent)
    store = plugin.state_store
    if change == "workspace":
        store.update_task_input(
            child.model_copy(update={"workspace": child.workspace.model_copy(update={"revision": "d" * 40})})
        )
    else:
        store.update_task_input(
            parent.model_copy(
                update={
                    "metadata": {
                        **parent.metadata,
                        "review_inventory": {**parent.metadata["review_inventory"], change: "changed"},
                    }
                }
            )
        )
    plugin.state_store = StateStore(store.db_path)
    plugin._validate_recovered_subtasks()
    assert plugin.state_store.task_status(child.task_id) == "failed"


@pytest.mark.anyio
@pytest.mark.parametrize("numbers", [[], [464], [447, 447], [-1]])
async def test_invalid_comparison_is_rejected_before_fetch_or_child_creation(comparison_review, numbers):
    plugin, parent, _, fetch, _ = comparison_review
    with pytest.raises(ValueError):
        await plugin.prepare_subtask(
            parent,
            SubtaskRequest(
                request_key="invalid", purpose="pr-compare", prompt="Compare", inputs={"related_prs": numbers}
            ),
        )
    fetch.assert_not_called()
    assert plugin.state_store.subtasks(parent.task_id) == []


@pytest.mark.anyio
async def test_comparison_does_not_own_or_freeze_correctness_admission(comparison_review):
    plugin, parent, _, _, _ = comparison_review
    child = await create_comparison(plugin, parent)
    inventory = parent.metadata["review_inventory"]
    group = {
        "files": [item["path"] for item in inventory["files"]],
        "category": "production",
        "decision": "accept",
        "reason": "Required behavior",
        "source": "PR requirements",
        "alternative": "Keep in repository",
    }
    plugin._scope_control(parent, {"inventory_id": inventory["inventory_id"], "groups": [group]})
    parent = plugin.state_store.task_request(parent.task_id)
    result = plugin._scope_control(
        parent,
        {"inventory_id": inventory["inventory_id"], "groups": [{**group, "decision": "clarify"}]},
    )
    assert result["scope"][inventory["inventory_id"]]["counts"]["clarify"] == len(group["files"])
    assert "review_scope" not in child.metadata["inputs"]


@pytest.mark.anyio
async def test_only_root_can_request_comparison(comparison_review):
    plugin, parent, _, fetch, _ = comparison_review
    with pytest.raises(ValueError, match="root reviewer"):
        await create_comparison(plugin, parent.model_copy(update={"spawned_by_task_id": "root"}))
    fetch.assert_not_called()
