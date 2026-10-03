from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from nyanpasu import __main__ as cli
from nyanpasu.models import TaskRunResult, TaskStatus
from tests.test_hybrid_memory import PUBLIC, config_for, make_agent, task
from tests.test_memory_pipeline import MemoryModel, publish_source


@pytest.mark.parametrize("fails", [False, True])
def test_rebuild_command_retries_navigation_and_reports_publication_result(tmp_path, monkeypatch, fails):
    config = config_for(tmp_path, consolidate=True)
    model = MemoryModel()
    if fails:
        model.fail_at.add(1)
    agent = make_agent(config, cheap=model)
    source = agent._admit(task("source"))
    agent.store.record_task(source)
    agent.store.mark_task_running(source.task_id, None)
    agent.store.mark_task_done(
        TaskRunResult(
            task_id=source.task_id,
            status=TaskStatus.COMPLETED,
            thread_id=None,
            turn_id=None,
            final_message="verified result",
        )
    )
    checkpoint = publish_source(agent.memory, PUBLIC, source.task_id)
    extraction = agent._memory_followup(source)
    assert extraction is not None
    failed = agent._memory_followup(extraction)
    assert failed is not None
    agent.store.record_task(failed)
    agent.store.mark_task_running(failed.task_id, None)
    agent.store.mark_task_failed(failed.task_id, "previous navigation failed")
    shutdown = AsyncMock(wraps=agent.shutdown)
    monkeypatch.setattr(agent, "shutdown", shutdown)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "AgentService", lambda _: agent)

    result = CliRunner().invoke(cli.app, ["memory-rebuild", source.task_id])

    assert result.exit_code == (1 if fails else 0), result.output
    status = json.loads(result.stdout)
    assert status["status"] == ("failed" if fails else "completed")
    assert status["kind"] == "memory_consolidation"
    assert status["task_id"] != failed.task_id
    assert agent.store.task_status(failed.task_id) == "failed"
    assert agent.memory.source_state(PUBLIC, source.task_id) == checkpoint
    assert [kind for kind, _ in model.events] == ["navigation"]
    navigation = agent.memory.list_navigation(PUBLIC)
    if fails:
        assert navigation == []
    else:
        assert navigation[0].sources == (checkpoint.id,)
    shutdown.assert_awaited_once()
