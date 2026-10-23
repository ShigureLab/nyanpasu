from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin
from nyanpasu_github_reviewer.prompt import build_review_prompt
from nyanpasu_github_reviewer.scope import review_source

HEAD = "a" * 40


@pytest.fixture
def split_review(tmp_path):
    config = NyanpasuConfig.model_validate(
        {
            "state_dir": str(tmp_path),
            "tasks": {"kinds": {"github_reviewer.pr-split": {"execution": {"model": "split-model"}}}},
        }
    )
    plugin = GitHubReviewerPlugin(GitHubReviewerConfig())
    plugin.runtime = Mock(config=config)
    plugin.state_store = StateStore(config.db_path)
    pr = PullRequestRef(
        repo="owner/repo",
        number=1,
        url="https://github.com/owner/repo/pull/1",
        base_ref="main",
        head_ref="feature",
        head_sha=HEAD,
        state="open",
        draft=False,
    )
    inventory = {
        "head_sha": HEAD,
        "base_ref": "main",
        "merge_base_sha": "b" * 40,
        "inventory_id": "c" * 64,
        "path_encoding": "percent",
        "files": [
            {"path": "api.py", "additions": 1200, "deletions": 600, "binary": False},
            {"path": "evidence/%FF.txt", "additions": 201, "deletions": 0, "binary": False},
        ],
    }
    parent = AgentTask(
        task_id="parent",
        context_key="github:owner/repo#1",
        action=TaskAction.RUN,
        prompt="Review",
        kind="github_reviewer.review",
        execution=config.resolve_execution("github_reviewer.review"),
        workspace=WorkspaceRef(key=pr.repo, local_path=tmp_path / "repo", revision=HEAD),
        metadata={"plugin_id": plugin.id, "pull_request": pr.model_dump(), "review_inventory": inventory},
    )
    plugin.state_store.record_task(parent)
    plugin.state_store.mark_task_running(parent.task_id, None)
    return plugin, parent, pr


@pytest.mark.parametrize("lines,required", [(1999, False), (2000, False), (2001, True)])
def test_split_trigger_counts_added_and_deleted_lines_at_strict_boundary(split_review, lines, required):
    plugin, parent, pr = split_review
    inventory = {
        **parent.metadata["review_inventory"],
        "files": [
            {"path": "api.py", "additions": 1200, "deletions": lines - 1200, "binary": False},
            {"path": "image.png", "additions": 0, "deletions": 0, "binary": True},
        ],
    }
    prompt = build_review_prompt(
        plugin.config, pr, "/worktree", runtime=parent.execution, triggers=(), inventory=inventory
    )

    assert ("PR split analysis required:" in prompt) is required
    assert f"Direct-base delta: {lines} changed lines" in prompt
    if not required:
        assert "behavior changes clearly separate" in prompt


async def create_split(plugin, parent):
    request = SubtaskRequest(
        request_key="split:current", purpose="pr-split", prompt="Separate API and evidence delivery"
    )
    prepared = await plugin.prepare_subtask(parent, request)
    return plugin.state_store.create_subtask(
        parent.task_id,
        request,
        prepared=prepared,
        execution=plugin.runtime.config.resolve_execution(prepared.kind, prepared.execution),
    )


@pytest.mark.anyio
async def test_split_child_sees_whole_delta_without_admission_and_does_not_freeze_it(split_review):
    plugin, parent, _ = split_review
    child = await create_split(plugin, parent)

    assert child.spawned_by_task_id == parent.task_id
    assert child.kind == "github_reviewer.pr-split"
    assert child.execution.model == "split-model"
    assert child.workspace.revision == HEAD
    assert child.workspace_mode == "clone"
    assert child.metadata["inputs"] == {"split_source": review_source(parent.metadata["review_inventory"])}
    assignment = json.loads(child.prompt.split("Pinned PR split assignment (JSON data):\n")[1])
    assert assignment["files"] == parent.metadata["review_inventory"]["files"]
    assert assignment["path_encoding"] == "percent"
    assert assignment["changed_lines"] == 2001
    assert "Separate API and evidence delivery" in child.prompt
    assert "Separate API and evidence delivery" not in child.developer_instructions

    group = {
        "files": ["api.py", "evidence/%FF.txt"],
        "category": "production",
        "decision": "accept",
        "reason": "Implements the requested behavior",
        "source": "PR requirement",
        "alternative": "Keep in repository",
    }
    plugin._scope_control(parent, {"inventory_id": "c" * 64, "groups": [group]})
    parent = plugin.state_store.task_request(parent.task_id)
    # Packaging analysis is allowed to influence admission, without owning correctness coverage.
    result = plugin._scope_control(
        parent,
        {"inventory_id": "c" * 64, "groups": [{**group, "decision": "relocate", "alternative": "PR artifacts"}]},
    )
    assert result["scope"]["c" * 64]["counts"]["relocate"] == 2
    plugin._validate_recovered_subtasks()
    assert plugin.state_store.task_status(child.task_id) == "queued"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["valid", "head", "base_ref", "merge_base_sha", "inventory_id", "workspace"])
async def test_split_recovery_rejects_stale_ranges_even_with_unchanged_head(split_review, change):
    plugin, parent, _ = split_review
    child = await create_split(plugin, parent)
    store = plugin.state_store
    if change == "workspace":
        store.update_task_input(
            child.model_copy(update={"workspace": child.workspace.model_copy(update={"revision": "d" * 40})})
        )
    elif change != "valid":
        key = "head_sha" if change == "head" else change
        inventory = {**parent.metadata["review_inventory"], key: "changed"}
        store.update_task_input(
            parent.model_copy(update={"metadata": {**parent.metadata, "review_inventory": inventory}})
        )
    plugin.state_store = StateStore(store.db_path)

    plugin._validate_recovered_subtasks()

    assert plugin.state_store.task_status(child.task_id) == ("queued" if change == "valid" else "failed")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change,error",
    [
        ("nested", "root reviewer"),
        ("inventory", "read review-scope"),
        ("revision", "inventory head"),
        ("inputs", "inputs must be empty"),
        ("instructions", "roles are supplied by the plugin"),
    ],
)
async def test_split_rejects_unowned_or_unpinned_requests(split_review, change, error):
    plugin, parent, _ = split_review
    values = {}
    if change == "nested":
        parent = parent.model_copy(update={"spawned_by_task_id": "root"})
    elif change == "inventory":
        parent = parent.model_copy(update={"metadata": {"pull_request": parent.metadata["pull_request"]}})
    elif change == "revision":
        values["revision"] = "d" * 40
    elif change == "inputs":
        values["inputs"] = {"review_files": ["api.py"]}
    else:
        values["developer_instructions"] = "Publish directly"

    with pytest.raises(ValueError, match=error):
        await plugin.prepare_subtask(
            parent,
            SubtaskRequest.model_validate(
                {"request_key": "split", "purpose": "pr-split", "prompt": "Split behaviors", **values}
            ),
        )
