from __future__ import annotations

import asyncio
import os
import subprocess
from unittest.mock import Mock
from urllib.parse import unquote_to_bytes

import pytest
from fastapi import FastAPI

from nyanpasu.agent import AgentService
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu.web import WebPluginRuntime
from nyanpasu_github_reviewer.models import GitHubReviewerConfig
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin
from nyanpasu_github_reviewer.scope import build_inventory, validate_plan
from tests.test_agent import FakeCodex, FakeWorktrees, _config, fake_backends
from tests.test_review_reference import reference_request, repository
from tests.test_subtask_runtime import wait_status


def scoped_task(tmp_path):
    repo, base, _, target = repository(tmp_path)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    git("checkout", "feature")
    (repo / "demos").mkdir()
    (repo / "demos/result.json").write_text('{"pass":true}')
    (repo / "helper.py").write_text("def helper(): return 1\n")
    git("add", ".")
    git("commit", "-m", "acceptance artifacts")
    task = AgentTask(
        task_id="review",
        context_key="review:1",
        action=TaskAction.RUN,
        prompt="review",
        workspace=WorkspaceRef(key="repo", local_path=repo, remote=str(repo), revision=git("rev-parse", "HEAD")),
        metadata={
            "plugin_id": "github_reviewer",
            "pull_request": {"head_sha": git("rev-parse", "HEAD"), "base_ref": "main"},
        },
    )
    return task, git, base, target


def test_inventory_includes_deleted_renamed_binary_and_unusual_paths(tmp_path):
    task, git, base, target = scoped_task(tmp_path)
    repo = task.workspace.local_path
    renamed = " leading\tname\n.txt"
    (repo / "contract.txt").rename(repo / renamed)
    (repo / "version.txt").unlink()
    (repo / "binary.bin").write_bytes(b"\x00\xff")
    git("add", "-A")
    git("commit", "-m", "mixed paths")
    head = git("rev-parse", "HEAD")
    task = task.model_copy(
        update={
            "workspace": task.workspace.model_copy(update={"revision": head}),
            "metadata": {**task.metadata, "pull_request": {"head_sha": head, "base_ref": "main"}},
        }
    )
    inventory = build_inventory(_config(tmp_path), task)
    files = {f["path"]: f for f in inventory["files"]}
    assert set(files) == {
        "author-only.txt",
        "helper.py",
        "demos/result.json",
        "contract.txt",
        renamed,
        "version.txt",
        "binary.bin",
    }
    assert files["binary.bin"]["binary"]
    assert files["contract.txt"]["deletions"] == files[renamed]["additions"] == 1
    assert inventory["merge_base_sha"] == base
    assert inventory["target_base_sha"] == target
    plan = {"inventory_id": inventory["inventory_id"], "groups": [group(list(files))]}
    assert renamed in validate_plan(inventory, plan).groups[0].files
    for paths in [list(files)[:-1], [*files, "unknown.py"], [*files, "helper.py"]]:
        with pytest.raises(ValueError, match="cover each changed file once"):
            validate_plan(inventory, {**plan, "groups": [group(paths)]})
    with pytest.raises(ValueError, match="another inventory"):
        validate_plan(inventory, {**plan, "inventory_id": "old-head"})


def group(files, decision="accept"):
    return {
        "files": files,
        "category": "production" if decision == "accept" else "evidence",
        "decision": decision,
        "reason": "Ship the implementation; archive one-off run results.",
        "source": "Feature request and repository callers",
        "alternative": "Fixed PR evidence archive",
    }


def test_non_utf8_inventory_paths_survive_persistence_without_collisions(tmp_path):
    task, git, _, _ = scoped_task(tmp_path)
    repo = task.workspace.local_path
    raw_paths = {b"file-\xff", b"file-%FF"}
    for path in raw_paths:
        (repo / os.fsdecode(path)).write_text("content\n")
    git("add", "-A")
    git("commit", "-m", "byte paths")
    head = git("rev-parse", "HEAD")
    task = task.model_copy(
        update={
            "workspace": task.workspace.model_copy(update={"revision": head}),
            "metadata": {**task.metadata, "pull_request": {"head_sha": head, "base_ref": "main"}},
        }
    )
    config = _config(tmp_path)
    inventory = build_inventory(config, task)
    assert inventory["path_encoding"] == "percent"
    paths = [item["path"] for item in inventory["files"]]
    assert {unquote_to_bytes(path) for path in paths} == raw_paths | {
        b"author-only.txt",
        b"helper.py",
        b"demos/result.json",
    }
    plan = validate_plan(inventory, {"inventory_id": inventory["inventory_id"], "groups": [group(paths)]})
    task = task.model_copy(
        update={"metadata": {**task.metadata, "review_inventory": inventory, "review_scope": plan.model_dump()}}
    )
    store = StateStore(config.db_path)
    store.record_task(task)
    recovered = store.task_request(task.task_id)
    assert recovered.metadata == task.metadata
    for path in recovered.metadata["review_scope"]["groups"][0]["files"]:
        assert (repo / os.fsdecode(unquote_to_bytes(path))).is_file()


@pytest.mark.anyio
async def test_scope_gate_persists_decisions_and_limits_all_child_roles(tmp_path):
    task, _, _, _ = scoped_task(tmp_path)
    config = _config(tmp_path)
    inventory = build_inventory(config, task)
    task = task.model_copy(update={"metadata": {**task.metadata, "review_inventory": inventory}})

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            await asyncio.Event().wait()

    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, Backend())
    )
    plugin = GitHubReviewerPlugin()
    plugin.runtime = Mock(config=config)
    agent.add_task_control_handler(plugin.id, plugin.scope_control)
    agent.add_subtask_preparer(plugin.id, plugin.prepare_subtask)
    try:
        await agent.submit(task)
        await wait_status(agent, task.task_id, "running")
        request = SubtaskRequest(
            request_key="module", prompt="review", purpose="module-review", inputs={"review_files": ["author-only.txt"]}
        )
        with pytest.raises(ValueError, match="complete review-scope"):
            await agent.control.dispatch(task.task_id, "create", request.model_dump())
        assert agent.store.subtasks(task.task_id) == []
        observed = await agent.control.dispatch(task.task_id, "review-scope", {})
        assert observed["inventory"] == inventory and observed["scope"] is None
        plan = {
            "inventory_id": inventory["inventory_id"],
            "groups": [group(["author-only.txt", "helper.py"]), group(["demos/result.json"], "relocate")],
        }
        report = await agent.control.dispatch(task.task_id, "review-scope", plan)
        assert report["scope"][inventory["head_sha"]]["counts"] == {"accept": 2, "relocate": 1, "clarify": 0}
        agent.store = StateStore(config.db_path)
        assert (await agent.control.dispatch(task.task_id, "review-scope", {}))["scope"] == report["scope"]
        for purpose in ["independent-design", "test-audit", "module-review", "custom-expert"]:
            rejected = request.model_copy(
                update={"purpose": purpose, "inputs": {"review_files": ["demos/result.json"]}}
            )
            with pytest.raises(ValueError, match="accepted files"):
                await agent.control.dispatch(task.task_id, "create", rejected.model_dump())
        assert agent.store.subtasks(task.task_id) == []
        # Pending placement also prevents dispatch, without blocking admitted work.
        plan["groups"][1]["decision"] = "clarify"
        await agent.control.dispatch(task.task_id, "review-scope", plan)
        with pytest.raises(ValueError, match="accepted files"):
            await agent.create_subtask(task.task_id, rejected)
        child = await agent.control.dispatch(task.task_id, "create", request.model_dump())
        saved = agent.store.task_request(child["task_id"])
        assert saved.workspace is not None
        assert saved.workspace.revision == inventory["head_sha"]
        assert child["inputs"]["review_scope"]["files"] == ["author-only.txt"]
        assert (await agent.control.dispatch(task.task_id, "create", request.model_dump()))["task_id"] == child[
            "task_id"
        ]
        with pytest.raises(ValueError, match="root reviewer"):
            await agent.control.dispatch(child["task_id"], "review-scope", plan)
        with pytest.raises(ValueError, match="accepted files"):
            await agent.create_subtask(
                child["task_id"], request.model_copy(update={"inputs": {"review_files": ["helper.py"]}})
            )
        with pytest.raises(ValueError, match="inventory head"):
            await agent.create_subtask(
                task.task_id, request.model_copy(update={"request_key": "wrong-version", "revision": "main"})
            )
        plan["groups"][1]["decision"] = "accept"
        with pytest.raises(ValueError, match="frozen"):
            await agent.control.dispatch(task.task_id, "review-scope", plan)
        independent = reference_request()
        independent = independent.model_copy(update={"inputs": {**independent.inputs, "review_files": ["helper.py"]}})
        reference = await agent.create_subtask(task.task_id, independent)
        assert reference.workspace is not None
        assert reference.workspace.revision == inventory["merge_base_sha"]
        assert "helper.py" not in reference.prompt + reference.developer_instructions
        grandchild = await agent.create_subtask(child["task_id"], request)
        assert grandchild.spawned_by_task_id == child["task_id"]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("scope_state", ["legacy", "unassigned", "stale", "accepted"])
async def test_restart_validates_scope_before_recovering_child_tree(tmp_path, scope_state):
    task, _, _, _ = scoped_task(tmp_path)
    config = _config(tmp_path).model_copy(update={"enabled_plugins": ("github_reviewer",)})
    inventory = build_inventory(config, task)
    if scope_state != "legacy":
        task = task.model_copy(
            update={
                "metadata": {
                    **task.metadata,
                    "review_inventory": inventory,
                    "review_scope": {
                        "inventory_id": inventory["inventory_id"],
                        "groups": [group([item["path"] for item in inventory["files"]])],
                    },
                }
            }
        )
    request = SubtaskRequest(request_key="old-child", prompt="child")
    if scope_state in {"accepted", "stale"}:
        request = request.model_copy(
            update={
                "inputs": {
                    "review_scope": {
                        "inventory_id": inventory["inventory_id"] if scope_state == "accepted" else "old-inventory",
                        "files": ["helper.py"],
                    }
                }
            }
        )
    started = {name: asyncio.Event() for name in ["review", "child", "grandchild"]}

    class Interrupted(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            started[kwargs["prompt"]].set()
            await asyncio.Event().wait()

    first = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, Interrupted())
    )
    try:
        await first.submit(task)
        await asyncio.wait_for(started["review"].wait(), 3)
        child = await first.create_subtask(task.task_id, request)
        await asyncio.wait_for(started["child"].wait(), 3)
        grandchild = await first.create_subtask(child.task_id, request.model_copy(update={"prompt": "grandchild"}))
        await asyncio.wait_for(started["grandchild"].wait(), 3)
        await first.wait_for_subtasks(child.task_id, [grandchild.task_id])
        await first.wait_for_subtasks(task.task_id, [child.task_id])
    finally:
        await first.shutdown()

    parent_context = first.store.get_context(task.context_key)
    assert parent_context is not None
    retried = []

    class Recovered(FakeCodex):
        async def run_turn(self, **kwargs):
            if kwargs["cwd"] == parent_context.session_worktree:
                retry = await second.create_subtask(task.task_id, request)
                retried.append(retry.task_id)
                assert retry.task_id == child.task_id
                assert second.store.task_status(retry.task_id) == (
                    "completed" if scope_state == "accepted" else "failed"
                )
            return await super().run_turn(**kwargs)

    backend = Recovered()
    second = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, backend)
    )
    plugin = GitHubReviewerPlugin()
    try:
        await plugin.setup(
            WebPluginRuntime(config=config, app=FastAPI(), agent=second),
            GitHubReviewerConfig(poll_enabled=False),
        )
        await second.startup()
        await wait_status(second, task.task_id, "completed")
        assert retried == [child.task_id]
        assert len(backend.calls) == (3 if scope_state == "accepted" else 1)
        assert second.store.task_status(grandchild.task_id) == (
            "completed" if scope_state == "accepted" else "cancelled"
        )
        assert second.store.unfinished_tasks() == []
    finally:
        await plugin.shutdown()
        await second.shutdown()
