from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nyanpasu.memory_consolidation import DEFAULT_MEMORY_POLICY, MemoryPolicy
from nyanpasu.models import TaskStatus
from nyanpasu.plugins import PluginManager, PluginRegistry
from nyanpasu.web import WebPluginRuntime
from tests.test_hybrid_memory import ALICE
from tests.test_memory_pipeline import MemoryModel, publish_source, publish_summary
from tests.test_memory_producer import batched_agent
from tests.test_rolling_memory import CONTEXT, continuing_task

if TYPE_CHECKING:
    from pydantic import BaseModel


@pytest.mark.anyio
async def test_plugin_policy_routes_extraction_and_summary_without_starting_plugin(tmp_path):
    class Plugin:
        id: str = "test"
        config_model: type[BaseModel] | None = None
        memory_policies = {"review": MemoryPolicy("SPECIAL EXTRACTION POLICY", "SPECIAL SUMMARY POLICY")}

        async def setup(self, runtime, config):
            pytest.fail("Policy registration must not start the plugin")

        async def shutdown(self):
            pass

    model = MemoryModel()
    agent = batched_agent(tmp_path, model, count=2)
    config = agent.config.model_copy(update={"plugins": agent.config.plugins.model_copy(update={"enabled": ("test",)})})
    manager = PluginManager(
        config, WebPluginRuntime(config=config, app=None, agent=agent), PluginRegistry({"test": Plugin()})
    )
    manager.register_memory_policies()
    try:
        await agent.run_now(
            continuing_task("first").model_copy(update={"kind": "review", "metadata": {"plugin_id": "test"}})
        )
        await agent.run_now(
            continuing_task("second").model_copy(update={"kind": "review", "metadata": {"plugin_id": "other"}})
        )
        job = await agent.rebuild_memory("first", fresh=True)
        assert (await agent.wait_for_memory(job.task_id)).status is TaskStatus.COMPLETED
        # Verify actual backend inputs at both stages, rather than a helper's rendered text.
        assert "SPECIAL EXTRACTION POLICY" in model.prompts[0]
        assert "SPECIAL SUMMARY POLICY" in model.prompts[1]
        assert agent._memory_policy(agent.store.task_request("second")) == DEFAULT_MEMORY_POLICY
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_batches_split_when_task_policies_differ(tmp_path):
    model = MemoryModel()
    agent = batched_agent(tmp_path, model, count=2)
    agent.add_memory_policy("first-plugin", "review", MemoryPolicy("FIRST POLICY", "FIRST SUMMARY"))
    agent.add_memory_policy("second-plugin", "review", MemoryPolicy("SECOND POLICY", "SECOND SUMMARY"))
    try:
        for name, plugin in [("first", "first-plugin"), ("second", "second-plugin")]:
            await agent.run_now(
                continuing_task(name).model_copy(update={"kind": "review", "metadata": {"plugin_id": plugin}})
            )
        job = await agent.rebuild_memory("second", fresh=True)
        assert (await agent.wait_for_memory(job.task_id)).status is TaskStatus.COMPLETED
        assert [kind for kind, _ in model.events] == ["extraction", "extraction", "summary"]
        for index, name in enumerate(["first", "second"]):
            assert model.events[index][1]["source_task_id"] == name
        assert "FIRST POLICY" in model.prompts[0] and "SECOND POLICY" not in model.prompts[0]
        assert "SECOND POLICY" in model.prompts[1] and "FIRST POLICY" not in model.prompts[1]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "extraction", "summary", "missing-history", "conflict"])
async def test_fresh_rebuild_replays_original_tasks_and_publishes_only_complete_pair(tmp_path, monkeypatch, failure):
    model = MemoryModel()
    agent = batched_agent(tmp_path, model, count=2)
    try:
        for name in ["first", "second"]:
            await agent.run_now(continuing_task(name))
        old = publish_source(
            agent.memory,
            ALICE,
            "second",
            context_key=CONTEXT,
            body="POLLUTED OLD WORKFLOW",
            completed_task_digests={"first": "old-first", "second": "old-second"},
        )
        previous_summary = publish_summary(
            agent.memory, ALICE, f"Old status [details](memory:{old.id})", context_key=CONTEXT
        )
        if failure == "missing-history":
            agent.backends.source("codex").turns = []
        if failure in {"extraction", "summary"}:
            model.fail_at.add(1 if failure == "extraction" else 2)
        run_turn = model.run_turn

        async def checked_run(**kwargs):
            assert agent.memory.source_state(ALICE, CONTEXT) == old
            assert agent.memory.snapshot_context(ALICE, CONTEXT, 1).summary == previous_summary
            result = await run_turn(**kwargs)
            if failure == "conflict" and len(model.events) == 2:
                publish_summary(
                    agent.memory, ALICE, f"Concurrent change [details](memory:{old.id})", context_key=CONTEXT
                )
            return result

        monkeypatch.setattr(model, "run_turn", checked_run)
        job = await agent.rebuild_memory("second", fresh=True)
        outcome = await agent.wait_for_memory(job.task_id)
        assert outcome.status is (TaskStatus.FAILED if failure else TaskStatus.COMPLETED)
        current = agent.memory.source_state(ALICE, CONTEXT)
        summary = agent.memory.snapshot_context(ALICE, CONTEXT, 1).summary
        if failure:
            assert current == old
            assert (
                summary == previous_summary if failure != "conflict" else summary.body.startswith("Concurrent change")
            )
        else:
            material = model.events[0][1]
            assert material["previous_account"] is None
            assert {item["source_task_id"] for item in material["evidence"]} == {"first", "second"}
            assert "POLLUTED OLD WORKFLOW" not in json.dumps(model.events)
            assert set(current.task_digests) == {"first", "second"}
            assert all(f"VERIFIED_LESSON_{index}" in current.body for index in [1, 2])
            assert summary is not None and not summary.stale and summary != previous_summary
            # Publishing again after a crash at the database completion boundary is idempotent.
            await agent._publish_memory_rebuild(agent.store.task_request(outcome.task_id))
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_empty_fresh_account_withdraws_polluted_memory_and_summary(tmp_path):
    model = MemoryModel()
    model.responses[1] = json.dumps({"title": "No substantive knowledge", "body": "", "topics": []})
    agent = batched_agent(tmp_path, model, count=1)
    try:
        await agent.run_now(continuing_task("first"))
        old = publish_source(agent.memory, ALICE, "first", context_key=CONTEXT, body="APPROVED dashboard rev 18")
        publish_summary(agent.memory, ALICE, f"Status [details](memory:{old.id})", context_key=CONTEXT)
        job = await agent.rebuild_memory("first", fresh=True)
        assert (await agent.wait_for_memory(job.task_id)).status is TaskStatus.COMPLETED
        assert agent.memory.source_state(ALICE, CONTEXT).body == ""
        assert agent.memory.snapshot_context(ALICE, CONTEXT, 1).summary.body == ""
        assert model.events[0][1]["previous_account"] is None
        assert [kind for kind, _ in model.events] == ["extraction"]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True])
async def test_plugin_feedback_is_collected_at_latest_contribution_and_failure_preserves_memory(tmp_path, fails):
    model = MemoryModel()
    agent = batched_agent(tmp_path, model, count=2)
    calls = []

    async def feedback(source, cutoff):
        calls.append((source.task_id, cutoff))
        if fails:
            raise RuntimeError("GitHub unavailable")
        return [
            {
                "kind": "github_feedback",
                "title": "Human maintainer advice",
                "origin": "github",
                "reference": "https://github.com/Org/Repo/pull/258#pullrequestreview-1",
                "blocks": ["SUBSTANTIVE_HUMAN_ADVICE_ABSENT_FROM_NATIVE_HISTORY"],
            }
        ]

    agent.add_memory_policy(
        "test", "review", MemoryPolicy("Capture full advice", "Summarize advice", evidence_provider=feedback)
    )
    try:
        for name in ["first", "second"]:
            await agent.run_now(
                continuing_task(name).model_copy(update={"kind": "review", "metadata": {"plugin_id": "test"}})
            )
        old = publish_source(agent.memory, ALICE, "second", context_key=CONTEXT, body="OLD STATUS")
        old_summary = publish_summary(agent.memory, ALICE, f"Old [status](memory:{old.id})", context_key=CONTEXT)
        job = await agent.rebuild_memory("second", fresh=True)
        result = await agent.wait_for_memory(job.task_id)
        assert result.status is (TaskStatus.FAILED if fails else TaskStatus.COMPLETED)
        assert all(name == "second" and cutoff == agent.store.task_run("second").updated_at for name, cutoff in calls)
        if fails:
            assert agent.memory.source_state(ALICE, CONTEXT) == old
            assert agent.memory.snapshot_context(ALICE, CONTEXT, 1).summary == old_summary
        else:
            assert len(calls) == 2  # Input freezing and execution use the same external evidence.
            material = model.events[0][1]
            external = [item for item in material["evidence"] if item["origin"] == "github"]
            assert len(external) == 1 and "SUBSTANTIVE_HUMAN_ADVICE" in external[0]["text"]
            assert external[0]["source_task_id"] == "second"
            assert external[0]["text"] in agent.memory.source_state(ALICE, CONTEXT).body
    finally:
        await agent.shutdown()
