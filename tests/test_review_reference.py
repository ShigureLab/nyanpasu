from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from nyanpasu.agent import AgentService
from nyanpasu.git_ops import WorktreeManager
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu_github_reviewer.reference import prepare_reference
from tests.test_agent import FakeCodex, _config, fake_backends
from tests.test_subtask_runtime import wait_status


def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (repo / "contract.txt").write_text("original user behavior")
    (repo / ".gitattributes").write_text("contract.txt export-ignore\nversion.txt export-subst\n")
    (repo / "version.txt").write_text("$Format:%H$")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "feature")
    (repo / "author-only.txt").write_text("implementation information")
    git("add", ".")
    git("commit", "-m", "author implementation")
    head = git("rev-parse", "HEAD")
    git("checkout", "main")
    (repo / "integration-only.txt").write_text("later target branch change")
    git("add", ".")
    git("commit", "-m", "advance base")
    target = git("rev-parse", "HEAD")
    return repo, base, head, target


def reference_request():
    return SubtaskRequest(
        request_key="reference:1",
        purpose="independent-design",
        prompt="Must be replaced",
        inputs={
            "reason": "lifecycle change",
            "requirements": [
                {"text": "Cancel the child before removing files", "source": "User request", "provenance": "explicit"}
            ],
        },
    )


@pytest.mark.anyio
async def test_independent_child_uses_common_base_without_author_history(tmp_path):
    repo, base, head, target = repository(tmp_path)
    config = _config(tmp_path, concurrency=1)
    parent_started, release_parent = asyncio.Event(), asyncio.Event()
    observed = {}

    class Backend(FakeCodex):
        async def run_turn(self, **kwargs):
            result = await super().run_turn(**kwargs)
            if kwargs["developer_instructions"].startswith("parent private history"):
                parent_started.set()
                await release_parent.wait()
                return result
            cwd = kwargs["cwd"]
            assert kwargs["thread_id"] is None
            assert (cwd / "contract.txt").read_text() == "original user behavior"
            assert (cwd / "version.txt").read_text() == "$Format:%H$"
            assert not (cwd / "author-only.txt").exists()
            assert not (cwd / "integration-only.txt").exists()
            assert "Must be replaced" not in kwargs["prompt"]
            assert "Cancel the child before removing files" in kwargs["prompt"]
            assert "parent private history" not in kwargs["developer_instructions"]
            remotes = subprocess.run(["git", "remote"], cwd=cwd, capture_output=True, text=True, check=True)
            assert remotes.stdout == ""
            assert subprocess.run(["git", "cat-file", "-e", head], cwd=cwd, capture_output=True).returncode != 0
            observed.update(json.loads((cwd / ".nyanpasu-source.json").read_text()))
            # The exported tree is a usable independent Git baseline for experiments.
            (cwd / "contract.txt").write_text("reference experiment")
            patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=cwd, capture_output=True, check=True).stdout
            assert b"+reference experiment" in patch
            return result

    agent = AgentService(config, worktrees=WorktreeManager(config), backends=fake_backends(config, Backend()))

    async def prepare(parent, request):
        return await asyncio.to_thread(prepare_reference, config, parent, request)

    agent.add_subtask_preparer("review", prepare)
    parent = AgentTask(
        task_id="parent",
        context_key="pr:1",
        action=TaskAction.RUN,
        prompt="parent",
        developer_instructions="parent private history",
        workspace=WorkspaceRef(key="repo", local_path=repo, remote=str(repo), revision=head),
        metadata={"plugin_id": "review", "pull_request": {"head_sha": head, "base_ref": "main"}},
    )
    try:
        await agent.submit(parent)
        await parent_started.wait()
        child = await agent.create_subtask("parent", reference_request())
        await wait_status(agent, child.task_id, "completed")
        manifest = agent.store.task_request(child.task_id).metadata["inputs"]
        assert manifest["merge_base_sha"] == observed["source_sha"] == base
        assert manifest["target_base_sha"] == target
        assert manifest["head_sha"] == head
        assert manifest["source_tree_sha"] == observed["source_tree_sha"]
        assert child.workspace_mode == "snapshot"
        assert await agent.create_subtask("parent", reference_request()) == child
        await agent.wait_for_subtasks("parent", [child.task_id])
        release_parent.set()
        await wait_status(agent, "parent", "completed")
    finally:
        await agent.shutdown()


def test_changed_requirement_invalidates_reference_digest_and_rejects_unsourced_input(tmp_path):
    repo, _, head, _ = repository(tmp_path)
    config = _config(tmp_path)
    parent = AgentTask(
        task_id="parent",
        context_key="pr:1",
        action=TaskAction.RUN,
        prompt="",
        workspace=WorkspaceRef(key="repo", local_path=repo, remote=str(repo), revision=head),
        metadata={"pull_request": {"head_sha": head, "base_ref": "main"}},
    )
    first = prepare_reference(config, parent, reference_request())
    altered = reference_request().model_dump()
    altered["inputs"]["requirements"][0]["text"] = "Preserve evidence after cleanup"
    second = prepare_reference(config, parent, SubtaskRequest.model_validate(altered))
    assert first.inputs["requirements_sha256"] != second.inputs["requirements_sha256"]
    assert first.revision == second.revision
    del altered["inputs"]["requirements"][0]["source"]
    with pytest.raises(ValueError):
        prepare_reference(config, parent, SubtaskRequest.model_validate(altered))


def test_reference_pins_target_even_when_another_fetch_overwrites_fetch_head(tmp_path, monkeypatch):
    repo, base, head, target = repository(tmp_path)
    config = _config(tmp_path)
    parent = AgentTask(
        task_id="parent",
        context_key="pr:1",
        action=TaskAction.RUN,
        prompt="",
        workspace=WorkspaceRef(key="repo", local_path=repo, remote=str(repo), revision=head),
        metadata={"pull_request": {"head_sha": head, "base_ref": "main"}},
    )
    run = subprocess.run

    def concurrent_fetch(argv, *args, **kwargs):
        result = run(argv, *args, **kwargs)
        if argv[:2] == ["git", "fetch"]:
            # A second review fetched its PR head into the same repository.
            (repo / ".git" / "FETCH_HEAD").write_text(head + "\n")
        return result

    monkeypatch.setattr(subprocess, "run", concurrent_fetch)
    prepared = prepare_reference(config, parent, reference_request())
    assert prepared.inputs["target_base_sha"] == target
    assert prepared.revision == base
