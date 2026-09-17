from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, call

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.agent import AgentService
from nyanpasu.claude import ClaudeBackend
from nyanpasu.config import ClaudeConfig, NyanpasuConfig, RuntimeConfig, load_config
from nyanpasu.models import AgentContext, AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.transcript.claude import ClaudeHistorySource, claude_history
from nyanpasu.web import create_app
from tests.claude_source import SESSION, records, write_session
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_agent import FakeWorktrees

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def configured(tmp_path: Path):
    return NyanpasuConfig(
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(backend="claude"),
        claude=ClaudeConfig(model="test-model", reasoning_effort="medium", allowed_tools=("Read",)),
    )


@pytest.fixture
def process(monkeypatch):
    async def respond(argv, *, input_text, received, **kwargs):
        message = json.loads(input_text)
        await received({"type": "system", "subtype": "init", "session_id": message["session_id"]})
        await received(
            {
                "type": "result",
                "subtype": "success",
                "session_id": message["session_id"],
                "is_error": False,
                "result": "done",
            }
        )
        return 0, ""

    process = AsyncMock(side_effect=respond)
    monkeypatch.setattr("nyanpasu.claude.JsonProcessRunner.run", process)
    return process


@pytest.mark.anyio
async def test_session_resume_and_updated_instructions(configured: NyanpasuConfig, tmp_path: Path, process):
    backend = ClaudeBackend(configured)
    started = AsyncMock()
    first = await backend.run_turn(cwd=tmp_path, prompt="first", thread_id=None, on_started=started)
    second = await backend.run_turn(
        cwd=tmp_path,
        prompt="second",
        thread_id=first.thread_id,
        developer_instructions="updated instructions",
        on_started=started,
    )
    assert first.thread_id == second.thread_id and first.turn_id != second.turn_id
    assert second.final_message == "done"
    assert started.await_args_list == [call(first.thread_id, first.turn_id), call(second.thread_id, second.turn_id)]
    requests = [json.loads(invocation.kwargs["input_text"]) for invocation in process.call_args_list]
    assert [message["uuid"] for message in requests] == [first.turn_id, second.turn_id]
    argv = process.call_args.args[0]
    assert argv[argv.index("--resume") + 1] == first.thread_id
    assert argv[argv.index("--append-system-prompt") + 1] == "updated instructions"
    assert argv[argv.index("--system-prompt-snapshot") + 1] == "off"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "test-model" in argv and "medium" in argv


@pytest.mark.anyio
@pytest.mark.parametrize("fail_before_start", [False, True])
async def test_backend_switch_never_resumes_old_thread(configured, tmp_path: Path, process, fail_before_start):
    agent = AgentService(configured, worktrees=FakeWorktrees(tmp_path / "worktrees"))
    task = AgentTask(task_id="old", context_key="switch", action=TaskAction.RUN, prompt="inspect")
    agent.store.record_task(task)
    agent.store.bind_task_execution("old", "old-codex", "old-turn", "codex")
    old_context = AgentContext(
        context_key="switch",
        backend="codex",
        thread_id="old-codex",
        session_worktree=tmp_path / "worktrees" / "switch",
        workspace_key=None,
        revision="old-head",
    )
    agent.store.upsert_context(old_context)
    try:
        if fail_before_start:
            respond = process.side_effect
            process.side_effect = RuntimeError("CLI unavailable")
            with pytest.raises(RuntimeError, match="CLI unavailable"):
                await agent.run_now(task.model_copy(update={"task_id": "failed"}))
            assert agent.store.get_context("switch") == old_context
            assert agent.store.task_backend("failed") == "claude"
            process.side_effect = respond
            process.reset_mock()
        first = await agent.run_now(task.model_copy(update={"task_id": "new"}))
        second = await agent.run_now(task.model_copy(update={"task_id": "resume"}))
        assert first.backend == second.backend == "claude"
        assert first.thread_id is not None
        assert first.thread_id == second.thread_id != "old-codex"
        start_argv, resume_argv = [invocation.args[0] for invocation in process.call_args_list]
        assert "--resume" not in start_argv
        assert start_argv[start_argv.index("--session-id") + 1] == first.thread_id
        assert resume_argv[resume_argv.index("--resume") + 1] == first.thread_id
        old_history = MemorySessionSource([turn("old-turn", tool("old-tool", "old conversation"))])
        app = create_app(configured, agent=agent, session_sources={"codex": old_history}.__getitem__)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            sessions = (await client.get("/api/sessions")).json()["items"]
            assert {row["session_id"] for row in sessions} == {"old-codex", "claude:" + first.thread_id}
            assert "old conversation" in (await client.get("/api/sessions/old-codex/export")).text
            assert (await client.get("/api/tasks/old")).json()["backend"] == "codex"
    finally:
        await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("args", [[], ["--profile", "agent profile"]])
async def test_configured_args_replace_optional_defaults(tmp_path: Path, monkeypatch, process, args):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[claude]\nbin = "/opt/agent wrapper"\nargs = {json.dumps(args)}\n')
    backend = ClaudeBackend(load_config())
    first = await backend.run_turn(cwd=tmp_path, prompt="first", thread_id=None)
    await backend.run_turn(
        cwd=tmp_path, prompt="second", thread_id=first.thread_id, developer_instructions="updated instructions"
    )
    for invocation, session_flag in zip(process.call_args_list, ("--session-id", "--resume"), strict=True):
        argv = invocation.args[0]
        assert argv[: 1 + len(args)] == ["/opt/agent wrapper", *args]
        assert "--permission-prompts" not in argv and "--system-prompt-snapshot" not in argv
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index(session_flag) + 1] == first.thread_id
    argv = process.call_args.args[0]
    assert argv[argv.index("--append-system-prompt") + 1] == "updated instructions"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "error"),
    [
        (None, "without a result"),
        ({"is_error": True, "result": "denied"}, "denied"),
        ({"session_id": "unrelated"}, "different session"),
        ({"result": None}, "missing its message"),
    ],
)
async def test_failed_results_preserve_session_binding(
    configured: NyanpasuConfig, tmp_path: Path, process, result, error
):
    async def respond(argv, *, input_text, received, **kwargs):
        session = json.loads(input_text)["session_id"]
        await received({"type": "system", "subtype": "init", "session_id": session})
        if result is not None:
            await received(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": session,
                    "is_error": False,
                    "result": "done",
                    **result,
                }
            )
        return 0, ""

    process.side_effect = respond
    agent = AgentService(configured, worktrees=FakeWorktrees(tmp_path / "worktrees"))
    try:
        with pytest.raises(RuntimeError, match=error):
            await agent.run_now(AgentTask(task_id="failed", context_key="test", action=TaskAction.RUN, prompt="run"))
        context = agent.store.get_context("test")
        assert context and context.backend == "claude" and context.thread_id
        task = agent.store.recent_tasks()[0]
        assert task.status == "failed" and task.backend == "claude"
    finally:
        await agent.shutdown()


def test_environment_is_explicit(configured: NyanpasuConfig, monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "not inherited")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not implicitly inherited")
    backend = ClaudeBackend(configured)
    assert "UNRELATED_SECRET" not in backend.env and "ANTHROPIC_API_KEY" not in backend.env


@pytest.mark.anyio
async def test_claude_dashboard_renders_native_tools_and_keeps_backend_namespaces(tmp_path: Path):
    home = tmp_path / "claude"
    path = write_session(home)
    config = NyanpasuConfig(state_dir=tmp_path / "state", claude=ClaudeConfig(env={"CLAUDE_CONFIG_DIR": str(home)}))
    state = StateStore(config.db_path)
    for backend in ("codex", "claude"):
        state.record_task(AgentTask(task_id=backend, context_key=backend, action=TaskAction.RUN, prompt="inspect"))
        state.bind_task_execution(backend, SESSION, "claude-input" if backend == "claude" else "turn", backend)
    codex = MemorySessionSource([turn("turn", tool("codex-tool", "Codex content"))])
    claude = ClaudeHistorySource({"CLAUDE_CONFIG_DIR": str(home)})
    app = create_app(config, session_sources={"codex": codex, "claude": claude}.__getitem__)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        sessions = (await client.get("/api/sessions")).json()["items"]
        assert {s["session_id"] for s in sessions} == {SESSION, "claude:" + SESSION}
        base = "/api/sessions/claude:" + SESSION
        detail = (await client.get(base)).json()
        assert detail["backend"] == detail["runtime"]["backend"] == "claude"
        assert detail["runtime"]["model"] == "claude-test-model"
        entries = (await client.get(base + "/transcript")).json()["entries"]
        assert all(e["task_id"] == "claude" for e in entries)
        command = next(e for e in entries if e["entry_id"] == "claude-bash")
        assert command["command"] == "pytest -q" and command["state"] == "failed"
        assert command["completed_at"] == "2026-09-14T00:00:03Z"
        assert any(e["kind"] == "reasoning" for e in entries)
        edit = next(e for e in entries if e["kind"] == "file_change")
        assert "verified explanation" in next(b["preview"] for b in edit["blocks"] if b["kind"] == "diff")
        hit = (await client.get(base + "/search", params={"q": "CLAUDE-NEEDLE"})).json()["items"][0]
        assert hit["entry_id"] == "claude-bash"
        assert "CLAUDE-NEEDLE" in (await client.get(base + "/content/" + hit["content_ref"] + "?download=true")).text
        assert "verified explanation" in (await client.get(base + "/export")).text
        assert (await client.get("/api/tasks/claude")).json()["session_id"] == "claude:" + SESSION
        assert (await client.get("/api/sessions/" + SESSION + "/content/" + hit["content_ref"])).status_code == 400
        path.unlink()
        assert (await client.get(base + "/transcript")).status_code == 503
        assert (await client.get(base)).json()["task_count"] == 1


def test_history_tracks_branches_compaction_and_redaction():
    data = records()
    # A discarded alternative must not be rendered in the current branch.
    alternative = {**data[-1], "uuid": "discarded", "message": {"content": "discarded branch"}}
    data.insert(-1, alternative)
    data.append(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "compact",
            "parentUuid": None,
            "logicalParentUuid": "claude-final",
            "sessionId": SESSION,
        }
    )
    data.append({**data[0], "uuid": "followup", "parentUuid": "compact", "message": {"content": "ghp_" + "a" * 30}})
    data.append({"type": "system", "uuid": "unrelated-metadata", "parentUuid": None})
    history = claude_history(SESSION, data)
    value = history.model_dump_json()
    assert "discarded branch" not in value and "Context compaction" in value
    assert "ghp_" not in value and "[REDACTED]" in value
    assert history.turns[-1].id == "followup"


def test_compaction_summary_does_not_create_a_task_turn_and_write_has_no_invented_diff():
    data = records()
    data.append(
        {
            **data[0],
            "uuid": "summary",
            "parentUuid": "claude-final",
            "isCompactSummary": True,
            "message": {"content": "Earlier context"},
        }
    )
    data.append(
        {
            **data[1],
            "uuid": "write-message",
            "parentUuid": "summary",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "write",
                        "name": "Write",
                        "input": {"file_path": "existing.py", "content": "updated file"},
                    }
                ]
            },
        }
    )
    history = claude_history(SESSION, data)
    assert len(history.turns) == 1
    assert history.turns[0].items[-2].presentation.kind == "compaction"
    write = history.turns[0].items[-1].presentation
    assert write.kind == "file_change" and write.state == "running"
    assert all(b.kind != "diff" for b in write.blocks)
    assert write.blocks[-1].text == "updated file"


@pytest.mark.anyio
async def test_partial_transcript_append_and_invalid_paths(tmp_path: Path):
    path = write_session(tmp_path)
    with path.open("a") as stream:
        stream.write('{"type":')
    source = ClaudeHistorySource({"CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert (await source.read_session(SESSION)).turns
    with pytest.raises(ValueError):
        await source.read_session("../../private")
    with path.open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError):
        await source.read_session(SESSION)


def test_existing_database_migrates_to_codex(tmp_path: Path):
    path = tmp_path / "old.db"
    StateStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE agent_contexts DROP COLUMN backend")
        conn.execute("ALTER TABLE task_runs DROP COLUMN backend")
        conn.execute("INSERT INTO agent_contexts VALUES ('old', 'thread', NULL, NULL, NULL, 1, 1)")
    state = StateStore(path)
    context = state.get_context("old")
    assert context and context.backend == "codex"
    assert context.thread_id == "thread"
