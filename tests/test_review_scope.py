from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
from unittest.mock import Mock
from urllib.parse import unquote_to_bytes

import pytest
from fastapi import FastAPI

from nyanpasu.agent import AgentService
from nyanpasu.config import PluginsConfig, TaskPolicy
from nyanpasu.control_tools import EmptyInput
from nyanpasu.models import AgentContext, AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu.web import WebPluginRuntime
from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef, RepoSettings, ReviewTrigger
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin
from nyanpasu_github_reviewer.pr_split import changed_line_count
from nyanpasu_github_reviewer.scope import (
    build_inventory,
    build_review_delta,
    review_source,
    scope_report,
    validate_plan,
)
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


@pytest.mark.anyio
async def test_re_review_uses_published_verified_range_and_delivers_removed_paths(tmp_path, monkeypatch):
    task, git, base, target = scoped_task(tmp_path)
    repo = task.workspace.local_path
    runtime = _config(tmp_path)
    previous = build_inventory(runtime, task)
    (repo / "helper.py").write_text("def helper(): return 2\n")
    (repo / "demos/result.json").unlink()
    git("add", "-A")
    git("commit", "-m", "address review")
    head = git("rev-parse", "HEAD")
    pr = PullRequestRef(
        repo="ExampleOrg/ExampleRepo",
        number=1,
        url="https://github.com/ExampleOrg/ExampleRepo/pull/1",
        base_ref="main",
        head_ref="feature",
        base_sha=target,
        head_sha=head,
        state="open",
        draft=False,
    )
    plugin = GitHubReviewerPlugin(
        GitHubReviewerConfig(repos={pr.repo: RepoSettings(local_path=repo, github_remote=str(repo))})
    )
    plugin.runtime = Mock(config=runtime)
    module = importlib.import_module("nyanpasu_github_reviewer.plugin")
    monkeypatch.setattr(module, "_fetch_pr", lambda *args: pr)
    finding = {"title": "Recheck callers", "status": "partial", "url": pr.url + "#discussion_r1"}
    prior_review = {
        "status": "changes_requested",
        "source": review_source(previous),
        "findings": {"F1": finding, "F2": {**finding, "status": "resolved"}},
    }
    read = Mock(return_value={"data": prior_review})
    monkeypatch.setattr(module, "read_review_dashboard", read)
    task = task.model_copy(
        update={
            "kind": "github_reviewer.review",
            "execution": runtime.resolve_execution("github_reviewer.review"),
            "metadata": {
                **task.metadata,
                "pull_request": pr.model_dump(mode="json"),
                "triggers": [ReviewTrigger(kind="pull_request_synchronize").model_dump(mode="json")],
            },
        }
    )
    context = AgentContext(
        context_key=task.context_key,
        thread_id="existing",
        revision="last-unverified-turn",
        session_worktree=repo,
        workspace_key=pr.repo,
    )
    prepared = await plugin.prepare_task(task, (), context)

    prompt_line = next(line for line in prepared.prompt.splitlines() if line.startswith("Changes since the last"))
    delta = json.loads(prompt_line.partition(": ")[2])
    assert delta["previous_source"] == review_source(previous)
    assert delta["previous_source"]["merge_base_sha"] == base
    assert {entry["path"] for entry in delta["files"]} == {"helper.py", "demos/result.json"}
    assert delta["open_findings"] == [{"id": "F1", **finding}]
    assert "last-unverified-turn" not in prepared.prompt
    inventory = prepared.metadata["review_inventory"]
    assert {entry["path"] for entry in inventory["files"]} == {"helper.py", "author-only.txt"}
    read.assert_called_once_with(plugin.config, pr)

    for old_review in (
        {**prior_review, "status": "preliminary"},
        {**prior_review, "source": {**prior_review["source"], "merge_base_sha": "0" * 40}},
        {**prior_review, "source": {**prior_review["source"], "inventory_id": "0" * 64}},
        {**prior_review, "source": {**prior_review["source"], "head_sha": "0" * 40}},
    ):
        assert build_review_delta(repo, inventory, old_review) is None
    unchanged = build_review_delta(repo, previous, prior_review)
    assert unchanged is not None and unchanged["files"] == []

    read.side_effect = ValueError("dashboard has visible drift")
    fallback = await plugin.prepare_task(task, (), context)
    assert fallback.action is TaskAction.RUN and fallback.metadata["review_inventory"] == inventory
    assert "Changes since the last verified review" not in fallback.prompt


@pytest.mark.anyio
async def test_stack_ranges_are_pinned_and_rechecked_before_publication(tmp_path, monkeypatch):
    task, git, trunk, _ = scoped_task(tmp_path)
    repo = task.workspace.local_path
    git("checkout", "-b", "lower", trunk)
    (repo / "lower.txt").write_text("lower v1\n" * 2001)
    git("add", ".")
    git("commit", "-m", "lower v1")
    lower = git("rev-parse", "HEAD")
    git("checkout", "-b", "upper")
    (repo / "upper.txt").write_text("upper\n")
    git("add", ".")
    git("commit", "-m", "upper")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/1/head", head)
    pr = PullRequestRef.from_github(
        "ExampleOrg/ExampleRepo",
        {
            "number": 1,
            "state": "open",
            "draft": False,
            "base": {"ref": "lower", "sha": lower},
            "head": {"ref": "upper", "sha": head},
            "stack": {"number": 7, "base": {"ref": "main"}},
        },
    )
    task = task.model_copy(
        update={
            "workspace": task.workspace.model_copy(update={"revision": head}),
            "metadata": {**task.metadata, "pull_request": pr.model_dump(mode="json")},
        }
    )
    runtime = _config(tmp_path)
    inventory = build_inventory(runtime, task)
    assert [item["path"] for item in inventory["files"]] == ["upper.txt"]
    assert changed_line_count(inventory) == 1  # The large lower layer must not trigger splitting this PR.
    plan = validate_plan(inventory, {"inventory_id": inventory["inventory_id"], "groups": [group(["upper.txt"])]})
    task = task.model_copy(update={"metadata": {**task.metadata, "review_inventory": inventory}})
    plugin = GitHubReviewerPlugin(
        GitHubReviewerConfig(
            repos={pr.repo: RepoSettings(local_path=repo, github_remote=str(repo), base_branches=("main",))}
        )
    )
    plugin.runtime = Mock(config=runtime)
    live = pr
    monkeypatch.setattr(importlib.import_module("nyanpasu_github_reviewer.plugin"), "_fetch_pr", lambda *args: live)
    assert (await plugin.review_verify_control(task, EmptyInput()))["source"] == review_source(inventory)

    # Advancing the direct base leaves the effective diff and its evidence intact.
    git("checkout", "lower")
    (repo / "later.txt").write_text("later lower change\n")
    git("add", ".")
    git("commit", "-m", "advance lower")
    live = pr.model_copy(update={"base_sha": git("rev-parse", "HEAD")})
    assert (await plugin.review_verify_control(task, EmptyInput()))["source"] == review_source(inventory)

    # Rewriting the lower layer changes the upper diff without changing its head.
    git("checkout", "-B", "lower", trunk)
    (repo / "lower.txt").write_text("lower v2\n")
    git("add", ".")
    git("commit", "-m", "rewrite lower")
    live = pr.model_copy(update={"base_sha": git("rev-parse", "HEAD")})
    # The original task remains pinned even after the named branch has moved.
    assert build_inventory(runtime, task) == inventory
    changed = build_inventory(
        runtime, task.model_copy(update={"metadata": {**task.metadata, "pull_request": live.model_dump(mode="json")}})
    )
    assert changed["head_sha"] == inventory["head_sha"]
    assert {item["path"] for item in changed["files"]} == {"lower.txt", "upper.txt"}
    assert changed["inventory_id"] not in scope_report(inventory, plan)
    with pytest.raises(ValueError, match="another inventory"):
        validate_plan(changed, plan.model_dump())
    with pytest.raises(ValueError, match="review range changed"):
        await plugin.review_verify_control(task, EmptyInput())

    # Retargeting to an equivalent ref must still re-establish eligibility/scope.
    live = pr.model_copy(update={"base_ref": "other"})
    with pytest.raises(ValueError, match="review range changed"):
        await plugin.review_verify_control(task, EmptyInput())

    git("checkout", "upper")
    (repo / "upper.txt").write_text("new upper\n")
    git("add", ".")
    git("commit", "-m", "advance upper")
    new_head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/1/head", new_head)
    live = pr.model_copy(update={"head_sha": new_head})
    with pytest.raises(ValueError, match="review range changed"):
        await plugin.review_verify_control(task, EmptyInput())


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
    store.record_task(task.model_copy(update={"execution": config.resolve_execution(task.kind)}))
    recovered = store.task_request(task.task_id)
    assert recovered.metadata == task.metadata
    for path in recovered.metadata["review_scope"]["groups"][0]["files"]:
        assert (repo / os.fsdecode(unquote_to_bytes(path))).is_file()


@pytest.mark.anyio
async def test_scope_gate_persists_decisions_and_limits_all_child_roles(tmp_path, monkeypatch):
    task, _, _, _ = scoped_task(tmp_path)
    config = _config(tmp_path)
    config.tasks.kinds["github_reviewer.codenip"] = TaskPolicy(
        execution={"model": "gpt-6.2-sol", "reasoning": "medium"}
    )
    config.tasks.kinds["github_reviewer.codenip-rule"] = TaskPolicy(
        execution={"model": "gpt-6-luna", "reasoning": "xhigh"}
    )
    monkeypatch.setattr(importlib.import_module("nyanpasu_github_reviewer.plugin"), "CODENIP_BATCH_SIZE", 1)
    skill = tmp_path / "skills" / "codenip" / "SKILL.md"
    rule = skill.parent / "rules/reuse-existing-operation/rule.md"
    pr_rule = skill.parent / "rules/check-pr-description/rule.md"
    rule.parent.mkdir(parents=True)
    pr_rule.parent.mkdir()
    skill.write_text(
        "| [REF001 · Reuse an existing operation](rules/reuse-existing-operation/rule.md) | Cross-language | ... |\n"
        "| [PR001 · Check PR description](rules/check-pr-description/rule.md) | PR description + diff | ... |\n"
    )
    rule.write_text("---\nid: REF001\n---\n\n# Reuse an existing operation\n")
    pr_rule.write_text("---\nid: PR001\n---\n\n# Check PR description\n")
    inventory = build_inventory(config, task)
    task = task.model_copy(update={"metadata": {**task.metadata, "review_inventory": inventory}})

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            await super().run_turn(**kwargs)
            await asyncio.Event().wait()

    agent = AgentService(
        config, worktrees=FakeWorktrees(tmp_path / "worktrees"), backends=fake_backends(config, Backend())
    )
    plugin = GitHubReviewerPlugin(GitHubReviewerConfig(codenip_skill=skill))
    plugin.runtime = WebPluginRuntime(config=config, app=None, agent=agent)
    plugin.state_store = agent.store
    agent.add_task_control_tools(plugin.id, plugin.task_control_tools())
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
        assert report["scope"][inventory["inventory_id"]]["counts"] == {"accept": 2, "relocate": 1, "clarify": 0}
        agent.store = StateStore(config.db_path)
        assert (await agent.control.dispatch(task.task_id, "review-scope", {}))["scope"] == report["scope"]
        for paths, error in [
            (["author-only.txt", "contract.txt"], r'accepted files: \["contract.txt"\]'),
            (["author-only.txt", "author-only.txt"], "remove duplicate path tokens"),
        ]:
            with pytest.raises(ValueError, match=error):
                await agent.control.dispatch(
                    task.task_id, "create", request.model_copy(update={"inputs": {"review_files": paths}}).model_dump()
                )
        assert agent.store.subtasks(task.task_id) == []
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
        coordinator = await agent.create_subtask(
            task.task_id,
            SubtaskRequest(
                request_key="codenip",
                purpose="codenip",
                prompt="Coordinate a rule review of the admitted helper.",
                inputs={"review_files": ["helper.py"], "pr_body": "Behavior is preserved."},
            ),
        )
        assert coordinator.kind == "github_reviewer.codenip"
        assert coordinator.execution is not None and coordinator.execution.model == "gpt-6.2-sol"
        await wait_status(agent, coordinator.task_id, "running")
        codenip = SubtaskRequest(
            request_key="codenip-ref001",
            purpose="codenip-rule",
            prompt="Check for a reusable operation in the admitted helper.",
            execution={"model": "other-model"},
            inputs={"review_files": ["helper.py"], "rule": str(rule.relative_to(skill.parent))},
        )
        with pytest.raises(ValueError, match="requested by the Codenip coordinator"):
            await agent.create_subtask(task.task_id, codenip)
        with pytest.raises(ValueError, match="only dispatches Codenip rule workers"):
            await agent.create_subtask(coordinator.task_id, request)
        with pytest.raises(ValueError, match="frozen Codenip rule assignments"):
            await agent.create_subtask(coordinator.task_id, codenip)
        with pytest.raises(ValueError, match="not available"):
            await agent.control.dispatch(task.task_id, "codenip-dispatch", {})
        batch = await agent.control.dispatch(coordinator.task_id, "codenip-dispatch", {})
        assert batch["total"] == 2 and batch["remaining"] == 1
        (first_id,) = batch["active_task_ids"]
        assert await agent.control.dispatch(coordinator.task_id, "codenip-dispatch", {}) == batch
        assert "PR description + diff" in agent.store.task_request(first_id).prompt
        assert "Behavior is preserved." in agent.store.task_request(first_id).prompt
        await agent.cancel_subtask(first_id)
        batch = await agent.control.dispatch(coordinator.task_id, "codenip-dispatch", {})
        assert batch["total"] == 2 and batch["remaining"] == 0
        (second_id,) = batch["active_task_ids"]
        audit = agent.store.task_request(second_id)
        assert audit.kind == "github_reviewer.codenip-rule" and audit.spawned_by_task_id == coordinator.task_id
        assert audit.execution is not None and (audit.execution.model, audit.execution.reasoning) == (
            "gpt-6-luna",
            "xhigh",
        )
        assert audit.workspace is not None and audit.workspace.revision == inventory["head_sha"]
        assert audit.metadata["inputs"]["rule_id"] == "REF001"
        assert audit.metadata["inputs"]["review_scope"]["files"] == ["helper.py"]
        assert "[experimental · REF001]" in audit.developer_instructions
        assert "Behavior is preserved." not in audit.prompt
        with pytest.raises(ValueError, match="workers cannot dispatch"):
            await agent.create_subtask(audit.task_id, codenip)
        await agent.cancel_subtask(second_id)
        assert await agent.control.dispatch(coordinator.task_id, "codenip-dispatch", {}) == {
            "total": 2,
            "remaining": 0,
            "active_task_ids": [],
        }
        assert (await agent.control.dispatch(task.task_id, "create", request.model_dump()))["task_id"] == child[
            "task_id"
        ]
        overlapping = await agent.create_subtask(
            task.task_id,
            request.model_copy(update={"request_key": "other-question", "prompt": "check another contract"}),
        )
        assert overlapping.task_id != child["task_id"]
        assert (
            overlapping.metadata["inputs"]["review_scope"]["files"] == saved.metadata["inputs"]["review_scope"]["files"]
        )
        with pytest.raises(ValueError, match="not available"):
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
        await wait_status(agent, child["task_id"], "running")
        grandchild = await agent.create_subtask(child["task_id"], request)
        assert grandchild.spawned_by_task_id == child["task_id"]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("scope_state", ["legacy", "unassigned", "stale", "accepted"])
async def test_restart_validates_scope_before_recovering_child_tree(tmp_path, scope_state):
    task, _, _, _ = scoped_task(tmp_path)
    config = _config(tmp_path).model_copy(update={"plugins": PluginsConfig(enabled=("github_reviewer",))})
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
