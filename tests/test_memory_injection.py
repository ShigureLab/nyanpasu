from __future__ import annotations

import json

import pytest

from nyanpasu.memory_context import MEMORY_CONTEXT_MAX_BYTES
from nyanpasu.models import TaskStatus
from tests.test_agent import FakeCodex
from tests.test_hybrid_memory import PUBLIC, config_for, make_agent, task


@pytest.mark.anyio
async def test_resumed_context_passes_only_latest_summary_and_keeps_each_turn_audit(tmp_path):
    """Verify the service-to-backend boundary, not native model history replacement."""
    worker = FakeCodex(new_session_id="worker-session")
    agent = make_agent(config_for(tmp_path), codex=worker)
    source = agent.memory.checkpoint_source(
        PUBLIC,
        "prior-task",
        context_key="shared",
        context_generation=1,
        source_order=0,
        input_digest="source-input",
        cursor=1,
        complete=True,
        title="Verified routing",
        body="Verified routing evidence.",
        topics=["routing"],
        sources=["tool:verification"],
    )
    versions = []
    audits = []
    try:
        for index, text in enumerate(("Original routing decision", "Revised routing decision")):
            snapshot = agent.memory.snapshot_context(PUBLIC, "shared", 1)
            summary = agent.memory.publish_summary(
                PUBLIC,
                "shared",
                1,
                body=f"{text}. [Evidence](memory:{source.id})",
                source_ids=[source.id],
                source_revisions=snapshot.source_revisions,
                input_digest=snapshot.input_digest,
                expected_revision=snapshot.summary.revision if snapshot.summary else None,
            )
            versions.append(summary)
            request = task(f"turn-{index}").model_copy(update={"context_key": "shared"})
            result = await agent.run_now(request)
            assert result.status is TaskStatus.COMPLETED
            records = agent.store.task_turns(request.task_id)
            assert len(records) == 1
            audit = json.loads(records[0]["memory_context_json"])
            audits.append(audit)
            assert audit["prompt"] in worker.instructions[index]
            assert audit["bytes"] == len(audit["prompt"].encode("utf-8")) <= MEMORY_CONTEXT_MAX_BYTES
            assert audit["selected"] == [{"id": summary.id, "revision": summary.revision, "reason": "current_context"}]
            assert summary.body in worker.instructions[index]

        assert worker.calls[0][1] is None and worker.calls[1][1] == "worker-session"
        assert versions[0].id == versions[1].id and versions[0].revision != versions[1].revision
        assert versions[0].body not in worker.instructions[1]
        assert versions[1].body not in worker.instructions[0]
        assert versions[0].body in audits[0]["prompt"] and versions[1].body in audits[1]["prompt"]
        assert json.loads(agent.store.task_turns("turn-0")[0]["memory_context_json"]) == audits[0]
    finally:
        await agent.shutdown()
