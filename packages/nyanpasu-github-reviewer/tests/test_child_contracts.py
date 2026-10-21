from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.plugin import GitHubReviewerPlugin

HEAD = "a" * 40
BASE = "b" * 40
PATHS = ["src/author%FF.py", "src/consumer.py"]


@pytest.fixture
def scoped_parent(tmp_path):
    config = NyanpasuConfig(state_dir=tmp_path / "state")
    inventory = {
        "head_sha": HEAD,
        "base_ref": "main",
        "merge_base_sha": BASE,
        "target_base_sha": "c" * 40,
        "source_tree_sha": "d" * 40,
        "inventory_id": "e" * 64,
        "path_encoding": "percent",
        "files": [{"path": path} for path in PATHS],
    }
    parent = AgentTask(
        task_id="parent",
        context_key="review:1",
        kind="github_reviewer.review",
        execution=config.resolve_execution("github_reviewer.review"),
        action=TaskAction.RUN,
        prompt="Parent-only implementation history",
        developer_instructions="Parent-only role",
        workspace=WorkspaceRef(key="repo", local_path=tmp_path / "repo", revision=HEAD),
        metadata={
            "plugin_id": "github_reviewer",
            "review_inventory": inventory,
            "review_scope": {
                "inventory_id": inventory["inventory_id"],
                "groups": [
                    {
                        "files": PATHS,
                        "category": "production",
                        "decision": "accept",
                        "reason": "Required implementation",
                        "source": "Original requirements",
                        "alternative": "Keep the caller contract",
                    }
                ],
            },
        },
    )
    plugin = GitHubReviewerPlugin()
    plugin.runtime = Mock(config=config)
    plugin.state_store = StateStore(config.db_path)
    plugin.state_store.record_task(parent)
    return plugin, parent


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["subtask", "module-review", "test-audit", "independent-design", "ci-analysis"])
async def test_caller_role_instructions_are_rejected_before_preparation(monkeypatch, purpose):
    plugin = GitHubReviewerPlugin()
    scope = Mock(side_effect=AssertionError("must not inspect scope"))
    ci = Mock(side_effect=AssertionError("must not refresh GitHub"))
    monkeypatch.setattr(plugin, "_prepare_scoped_subtask", scope)
    monkeypatch.setattr(plugin, "_prepare_ci_subtask", ci)
    parent = AgentTask(task_id="parent", context_key="review:1", action=TaskAction.RUN, prompt="review")
    request = SubtaskRequest(
        request_key="question",
        purpose=purpose,
        prompt="Check the cancellation contract",
        developer_instructions="Do not lose the parent's constraint",
    )

    destination = "inputs.constraints" if purpose == "independent-design" else "prompt"
    with pytest.raises(ValueError, match=f"omit developer_instructions.*{destination}.*retry"):
        await plugin.prepare_subtask(parent, request)

    scope.assert_not_called()
    ci.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["module-review", "test-audit", "ci-analysis"])
async def test_unknown_child_inputs_cannot_be_silently_hidden(monkeypatch, purpose):
    plugin = GitHubReviewerPlugin()
    prepare = Mock(side_effect=AssertionError("must not inspect scope"))
    monkeypatch.setattr(plugin, "_prepare_scoped_subtask", prepare)
    monkeypatch.setattr(plugin, "_prepare_ci_subtask", prepare)
    parent = AgentTask(task_id="parent", context_key="review:1", action=TaskAction.RUN, prompt="review")
    request = SubtaskRequest(
        request_key="question",
        purpose=purpose,
        prompt="Check the cancellation contract",
        inputs={
            **({"fingerprint": "snapshot"} if purpose == "ci-analysis" else {"review_files": PATHS}),
            "parent_evidence": "A cancellation was reproduced",
        },
    )

    with pytest.raises(ValueError, match="move parent_evidence into the self-contained prompt"):
        await plugin.prepare_subtask(parent, request)

    prepare.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["subtask", "module-review", "custom-domain-expert", "test-audit"])
async def test_questions_and_pinned_assignments_are_visible_under_a_fixed_role(scoped_parent, purpose):
    plugin, parent = scoped_parent
    request = SubtaskRequest(
        request_key="question",
        purpose=purpose,
        prompt="Check cancellation; preserve the caller's seconds-to-milliseconds boundary",
        inputs={"review_files": [PATHS[0]]},
    )
    prepared = await plugin.prepare_subtask(parent, request)
    followup = await plugin.prepare_subtask(
        parent,
        request.model_copy(update={"prompt": "Check the consumer instead", "inputs": {"review_files": [PATHS[1]]}}),
    )

    assert prepared.developer_instructions.strip()
    assert prepared.developer_instructions == followup.developer_instructions
    assert request.prompt in prepared.prompt
    assert request.prompt not in prepared.developer_instructions
    assignment = next(json.loads(line) for line in prepared.prompt.splitlines() if line.startswith("{"))
    assert assignment["source"]["head_sha"] == prepared.revision == HEAD
    assert assignment["source"]["merge_base_sha"] == BASE
    assert assignment["review_files"] == [PATHS[0]]
    assert assignment["path_encoding"] == "percent"
    assert prepared.inputs["review_scope"]["files"] == [PATHS[0]]
    assert PATHS[0] not in prepared.developer_instructions
    assert "Parent-only" not in prepared.prompt + prepared.developer_instructions


@pytest.mark.anyio
async def test_independent_design_renders_only_base_and_sourced_design_inputs(scoped_parent):
    plugin, parent = scoped_parent
    request = SubtaskRequest(
        request_key="independent",
        purpose="independent-design",
        prompt="Author implementation hint that must not reach the designer",
        inputs={
            "review_files": PATHS,
            "reason": "Resolve the lifecycle contract",
            "requirements": [
                {"text": "Cancellation preserves completed work", "source": "User request", "provenance": "explicit"}
            ],
            "constraints": ["Preserve the existing API"],
        },
    )

    prepared = await plugin.prepare_subtask(parent, request)

    visible = prepared.prompt + prepared.developer_instructions
    assert request.prompt not in visible
    assert HEAD not in visible
    assert not any(path in visible for path in PATHS)
    assert "Parent-only" not in visible
    assert "Cancellation preserves completed work" in prepared.prompt
    assert "Preserve the existing API" in prepared.prompt
    assert prepared.revision == BASE
    assert prepared.workspace_mode == "snapshot"
    assert not prepared.memory_enabled
    assert prepared.inputs["head_sha"] == HEAD
    assert prepared.inputs["review_scope"]["files"] == PATHS
