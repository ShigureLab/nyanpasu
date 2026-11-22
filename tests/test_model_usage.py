from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from nyanpasu.config import NyanpasuConfig, ServerConfig
from nyanpasu.models import AgentTask, SubtaskRequest, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.targets import ExecutionOverride
from nyanpasu.transcript.codex import zstd
from nyanpasu.transcript.models import ModelUsageTotals
from nyanpasu.transcript.pricing import estimate_api_cost
from nyanpasu.web import create_app
from tests.claude_source import SESSION, write_session

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_model_usage_reads_native_history_and_refreshes(tmp_path: Path):
    codex, claude = tmp_path / "codex", tmp_path / "claude"
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        server=ServerConfig(token=SecretStr("fixture-token")),
        backends={
            "codex": {"driver": "codex", "process": {"env": {"CODEX_HOME": str(codex)}}},
            "claude": {"driver": "claude-code", "process": {"env": {"CLAUDE_CONFIG_DIR": str(claude)}}},
        },
    )
    store = StateStore(config.db_path)
    thread = "cbdbbc63-61bc-4d3c-931b-1cabec7f9891"
    task = AgentTask(
        task_id="root",
        context_key="project:review",
        action=TaskAction.RUN,
        prompt="Check",
        kind="review",
        execution=config.resolve_execution(),
    )
    store.record_task(task)
    store.mark_task_running(task.task_id, None)
    store.bind_task_execution(task.task_id, thread, "modern")
    subtask = store.create_subtask(
        task.task_id,
        SubtaskRequest(request_key="child", prompt="Check a module", purpose="module-review", kind="review.audit"),
        execution=config.resolve_execution(override=ExecutionOverride(backend="claude")),
    )
    store.bind_task_execution(subtask.task_id, SESSION, "claude-turn", "claude")
    missing = AgentTask(
        task_id="missing",
        context_key="missing",
        action=TaskAction.RUN,
        prompt="Check",
        execution=config.resolve_execution(),
    )
    store.record_task(missing)
    store.bind_task_execution(missing.task_id, "32ebdbc4-816b-4dba-a532-bb1bde0c9747", "missing-turn")

    now = datetime.now(UTC)

    def stamp(days: float) -> str:
        return (now - timedelta(days=days)).isoformat()

    def codex_record(kind: str, **payload) -> dict:
        return {"type": kind, "timestamp": stamp(0.2), "payload": payload}

    def token_count(days: float, input_tokens: int, cached: int, output: int) -> dict:
        return {
            **codex_record(
                "event_msg",
                type="token_count",
                info={
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached,
                        "output_tokens": output,
                    }
                },
            ),
            "timestamp": stamp(days),
        }

    def response(response_id: str, input_tokens: int, cached: int, output: int) -> dict:
        return codex_record(
            "token_usage_record",
            thread_id=thread,
            turn_id="modern",
            response_id=response_id,
            usage={"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output},
        )

    def encode(*records: dict) -> bytes:
        return "".join(json.dumps(record) + "\n" for record in records).encode()

    data = encode(
        codex_record("session_meta", id=thread, model_provider="openai"),
        codex_record("turn_context", turn_id="older", model="codex-archive"),
        token_count(31, 3, 1, 1),
        codex_record("turn_context", turn_id="legacy", model="codex-old"),
        token_count(29, 13, 6, 3),
        token_count(2, 33, 11, 6),
        token_count(2, 33, 11, 6),
        codex_record("event_msg", type="task_started", turn_id="modern"),
        response("compaction", 4, 0, 1),
        codex_record("turn_context", turn_id="modern", model="gpt-6.2-sol"),
        token_count(0.2, 33, 11, 6),
        response("normal", 100, 60, 9),
        response("normal", 100, 60, 9),
        token_count(0.2, 133, 71, 15),
    )
    archive = codex / "archived_sessions" / f"rollout-2026-01-01T00-00-00-{thread}.jsonl.zst"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(zstd.compress(data))
    unrelated = archive.with_name("rollout-2026-01-01T00-00-00-82bca46e-d8bb-4d05-a284-fd123dbcd0c8.jsonl")
    unrelated.write_bytes(data)

    def claude_message(output: int) -> dict:
        return {
            "type": "assistant",
            "sessionId": SESSION,
            "timestamp": stamp(0.1),
            "message": {
                "id": "response",
                "model": "claude-model",
                "usage": {
                    "input_tokens": 40,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": output,
                },
            },
        }

    older_message = {
        **claude_message(5),
        "timestamp": stamp(15),
        "message": {"id": "older", "model": "claude-older", "usage": {"input_tokens": 180, "output_tokens": 5}},
    }
    transcript = write_session(claude, [older_message, claude_message(4), claude_message(5)])
    transport = ASGITransport(app=create_app(config))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/model-usage")).status_code == 401
        client.headers["Authorization"] = "Bearer fixture-token"
        response_data = (await client.get("/api/model-usage")).json()
        assert response_data["unavailable_sessions"] == 1
        periods = response_data["periods"]
        day = periods["last_24h"]
        week = periods["last_7d"]
        month = periods["last_30d"]
        assert {
            key: day[key]
            for key in (
                "calls",
                "cached_calls",
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
            )
        } == {
            "calls": 3,
            "cached_calls": 2,
            "input_tokens": 204,
            "cached_input_tokens": 110,
            "cache_write_tokens": 10,
            "output_tokens": 15,
        }
        assert day["cost_usd"] == pytest.approx(0.000194)
        assert day["priced_calls"] == 2
        assert week["input_tokens"] == 224
        assert month["input_tokens"] == 414
        assert periods["all_time"]["input_tokens"] == 417
        assert month["cost_usd"] == pytest.approx(day["cost_usd"])
        assert periods["all_time"]["priced_calls"] == 2
        assert {agent["agent"]: agent["input_tokens"] for agent in day["by_agent"]} == {
            "review": 104,
            "review.audit": 100,
        }
        assert [agent["agent"] for agent in month["by_agent"]] == ["review.audit", "review"]
        assert sum(agent["input_tokens"] for agent in month["by_agent"]) == month["input_tokens"]
        assert {model["model"]: model["input_tokens"] for model in day["by_model"]} == {
            "gpt-6.2-sol": 104,
            "claude-model": 100,
        }
        assert {model["model"]: model["input_tokens"] for model in week["by_model"]} == {
            "gpt-6.2-sol": 104,
            "codex-old": 20,
            "claude-model": 100,
        }
        assert {model["model"]: model["input_tokens"] for model in month["by_model"]} == {
            "gpt-6.2-sol": 104,
            "codex-old": 30,
            "claude-model": 100,
            "claude-older": 180,
        }
        assert [item["session_id"] for item in day["top_sessions"]] == [thread, f"claude:{SESSION}"]
        assert [item["session_id"] for item in month["top_sessions"]] == [f"claude:{SESSION}", thread]
        assert day["top_sessions"][0]["cost_usd"] == pytest.approx(day["cost_usd"])
        assert day["top_sessions"][1]["cost_usd"] is None
        assert response_data["top_sessions"] == day["top_sessions"]
        assert response_data["by_model"] == week["by_model"]

        appended = encode(claude_message(8))
        with transcript.open("ab") as stream:
            stream.write(appended[:-1])
        assert (await client.get("/api/model-usage")).json() == response_data
        with transcript.open("ab") as stream:
            stream.write(appended[-1:])
        refreshed = (await client.get("/api/model-usage")).json()
        assert refreshed["periods"]["last_24h"]["calls"] == 3
        assert refreshed["periods"]["last_24h"]["output_tokens"] == 18


def test_estimated_api_cost_uses_provider_rates_and_openai_prompt_size():
    usage = ModelUsageTotals(
        input_tokens=272_000, cached_input_tokens=260_000, cache_write_tokens=1_000, output_tokens=1_000
    )
    assert estimate_api_cost("openai", "gpt-6.2-sol", usage) == pytest.approx(0.0605)
    long_prompt = usage.model_copy(update={"input_tokens": 272_001})
    assert estimate_api_cost("openai", "gpt-6.2-sol", long_prompt) == pytest.approx(0.116004)
    assert estimate_api_cost("deepseek", "deepseek-flash", usage) == pytest.approx(0.00636)
    assert estimate_api_cost("proxy", "gpt-6.2-sol", usage) is None
    assert estimate_api_cost("openai", "unknown-model", usage) is None
