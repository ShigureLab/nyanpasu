from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from nyanpasu.agent import AgentService
from nyanpasu.claude import ClaudeBackend
from nyanpasu.config import ClaudeConfig, CodexConfig, NyanpasuConfig, RuntimeConfig, load_config
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu.store import StateStore
from nyanpasu.transcript.claude import ClaudeHistorySource, claude_history
from nyanpasu.transcript.queries import TranscriptReader
from nyanpasu.web import create_app
from tests.claude_source import SESSION, records, write_session
from tests.session_source import MemorySessionSource, tool, turn
from tests.test_agent import FakeWorktrees


@pytest.fixture
def configured(tmp_path: Path):
    # The wrapper accepts an argument containing shell metacharacters, then speaks the real protocol.
    wrapper = tmp_path / "runtime wrapper.py"
    wrapper.write_text("""import json,os,sys,time,uuid
from pathlib import Path
assert sys.argv[1] == 'literal $(touch SHOULD_NOT_EXIST)'
event=json.loads(sys.stdin.readline())
sid=event['session_id']; uid=event['uuid']; prompt=event['message']['content']
resume='--resume' in sys.argv
home=Path(os.environ['CLAUDE_CONFIG_DIR'])
path=home/'projects'/'test'/(sid+'.jsonl'); path.parent.mkdir(parents=True,exist_ok=True)
old=[json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
assert bool(old)==resume
if prompt=='wait':
    (home/'pid').write_text(str(os.getpid()))
def send(e): print(json.dumps(e),flush=True)
send({'type':'system','subtype':'init','session_id':sid})
def record(role,id,blocks,parent):
    return {'type':role,'uuid':id,'parentUuid':parent,'sessionId':sid,'timestamp':'2026-09-14T00:00:00Z','cwd':str(Path.cwd()),'version':'test', 'message':{'role':role,'content':blocks}}
with path.open('a') as f:
    f.write(json.dumps(record('user',uid,prompt,old[-1]['uuid'] if old else None))+'\\n')
    f.flush()
    if prompt=='wait': time.sleep(120)
    f.write(json.dumps(record('assistant',str(uuid.uuid4()),'answer '+prompt,uid))+'\\n')
with (home/'argv.jsonl').open('a') as f: f.write(json.dumps(sys.argv)+'\\n')
if prompt=='flood':
    sys.stderr.write('stderr flood\\n'*30000);sys.stderr.flush()
if prompt=='empty': sys.exit(0)
if prompt=='wrong-session': sid=str(uuid.uuid4())
if prompt=='invalid-result':
    send({'type':'result','subtype':'success','session_id':sid,'is_error':False});sys.exit(0)
send({'type':'result','subtype':'success','session_id':sid,'is_error':prompt=='fail','result':'denied' if prompt=='fail' else 'answer '+prompt})
""")
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(backend="claude"),
        claude=ClaudeConfig(
            bin=sys.executable,
            args=(str(wrapper), "literal $(touch SHOULD_NOT_EXIST)"),
            env={"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")},
            model="test-model",
            reasoning_effort="medium",
            allowed_tools=("Bash(pytest *)",),
        ),
    )
    return config


@pytest.mark.anyio
async def test_wrapper_execution_resume_task_binding_and_native_history(configured: NyanpasuConfig, tmp_path: Path):
    agent = AgentService(configured, worktrees=FakeWorktrees(tmp_path / "worktrees"))
    first = await agent.run_now(
        AgentTask(
            task_id="first",
            context_key="test",
            action=TaskAction.RUN,
            prompt="first",
            developer_instructions="original instructions",
        )
    )
    second = await agent.run_now(
        AgentTask(
            task_id="second",
            context_key="test",
            action=TaskAction.RUN,
            prompt="second",
            developer_instructions="updated instructions",
        )
    )
    assert first.backend == second.backend == "claude"
    assert first.thread_id == second.thread_id
    assert first.turn_id != second.turn_id
    assert first.thread_id
    UUID(first.thread_id)
    reader = TranscriptReader(agent.store.db_path, agent.backends.source)
    key = "claude:" + first.thread_id
    assert reader.sessions()["items"][0]["session_id"] == key
    window = await reader.window(key)
    assert [e["task_id"] for e in window["entries"]] == ["first", "first", "second", "second"]
    assert [e["turn_id"] for e in window["entries"]] == [first.turn_id, first.turn_id, second.turn_id, second.turn_id]
    argv = [json.loads(line) for line in (tmp_path / "claude" / "argv.jsonl").read_text().splitlines()]
    assert "--session-id" in argv[0] and "--resume" in argv[1]
    assert "updated instructions" in argv[1][argv[1].index("--append-system-prompt") + 1]
    assert argv[1][argv[1].index("--system-prompt-snapshot") + 1] == "off"
    assert argv[1][argv[1].index("--permission-prompts") + 1] == "none"
    assert "test-model" in argv[1] and "medium" in argv[1]
    assert not list(tmp_path.rglob("SHOULD_NOT_EXIST"))
    await agent.shutdown()

    # Changing the default preserves existing context ownership and still permits fresh Claude work.
    changed = configured.model_copy(update={"runtime": RuntimeConfig(backend="codex")})
    resumed = AgentService(changed, worktrees=FakeWorktrees(tmp_path / "worktrees"))
    third = await resumed.run_now(AgentTask(task_id="third", context_key="test", action=TaskAction.RUN, prompt="third"))
    assert third.backend == "claude" and third.thread_id == first.thread_id
    assert set(resumed.backends._instances) == {"claude"}
    await resumed.run_now(AgentTask(task_id="cleanup", context_key="test", action=TaskAction.CLEANUP, prompt=""))
    assert resumed.store.get_context("test") is None
    assert (await reader.window(key))["entries"]
    await resumed.shutdown()


@pytest.mark.anyio
async def test_result_error_and_failure_binding_survive_restart(configured: NyanpasuConfig, tmp_path: Path):
    agent = AgentService(configured, worktrees=FakeWorktrees(tmp_path / "worktrees"))
    with pytest.raises(RuntimeError, match="denied"):
        await agent.run_now(AgentTask(task_id="failed", context_key="test", action=TaskAction.RUN, prompt="fail"))
    context = agent.store.get_context("test")
    assert context and context.backend == "claude" and context.thread_id
    assert agent.store.recent_tasks()[0].status == "failed"
    assert agent.store.recent_tasks()[0].backend == "claude"
    await agent.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_stop_kills_running_cli(configured: NyanpasuConfig, tmp_path: Path, stop: str):
    if stop == "timeout":
        configured = configured.model_copy(
            update={"claude": configured.claude.model_copy(update={"command_timeout_seconds": 1})}
        )
    backend = ClaudeBackend(configured)
    running = asyncio.create_task(backend.run_turn(cwd=tmp_path, prompt="wait", thread_id=None))
    pidfile = tmp_path / "claude" / "pid"
    async with asyncio.timeout(3):
        while not pidfile.exists():
            await asyncio.sleep(0.01)
    assert backend.runtime_info()["connection"] == "running"
    if stop == "cancel":
        await backend.close()
    with pytest.raises(TimeoutError if stop == "timeout" else asyncio.CancelledError):
        await running
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert backend.runtime_info()["connection"] == "idle"


@pytest.mark.anyio
async def test_stderr_is_drained_and_environment_is_explicit(configured: NyanpasuConfig, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "not inherited")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not implicitly inherited")
    backend = ClaudeBackend(configured)
    assert "UNRELATED_SECRET" not in backend.env and "ANTHROPIC_API_KEY" not in backend.env
    assert (await backend.run_turn(cwd=tmp_path, prompt="flood", thread_id=None)).final_message == "answer flood"
    assert backend.runtime_info()["diagnostics"]
    await backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prompt", "error"),
    [("empty", "without a result"), ("wrong-session", "different session"), ("invalid-result", "missing its message")],
)
async def test_incomplete_or_unrelated_results_fail(configured: NyanpasuConfig, tmp_path: Path, prompt, error):
    backend = ClaudeBackend(configured)
    with pytest.raises(RuntimeError, match=error):
        await backend.run_turn(cwd=tmp_path, prompt=prompt, thread_id=None)
    await backend.close()


@pytest.mark.anyio
async def test_startup_failure_preserves_stderr_with_large_input(tmp_path: Path):
    backend = ClaudeBackend(
        NyanpasuConfig(
            state_dir=tmp_path,
            claude=ClaudeConfig(
                bin=sys.executable,
                args=("-c", "import sys; sys.stderr.write('unknown wrapper profile'); sys.exit(2)"),
            ),
        )
    )
    with pytest.raises(RuntimeError, match="unknown wrapper profile"):
        await backend.run_turn(cwd=tmp_path, prompt="input" * 100000, thread_id=None)
    await backend.close()


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != "linux", reason="Checks the process group through procfs")
async def test_timeout_cleans_children_after_wrapper_exits(tmp_path: Path):
    pidfile = tmp_path / "child.pid"
    code = "import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); Path('child.pid').write_text(str(p.pid))"
    backend = ClaudeBackend(
        NyanpasuConfig(
            state_dir=tmp_path,
            claude=ClaudeConfig(
                bin=sys.executable,
                args=("-c", code),
                command_timeout_seconds=1,
            ),
        )
    )
    with pytest.raises(TimeoutError):
        await backend.run_turn(cwd=tmp_path, prompt="inspect", thread_id=None)
    status = Path("/proc") / pidfile.read_text() / "stat"
    async with asyncio.timeout(3):
        while status.exists() and status.read_text().split()[2] != "Z":
            await asyncio.sleep(0.01)
    await backend.close()


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


def test_runtime_config_environment_and_custom_bins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    monkeypatch.setenv("NYANPASU_BACKEND", "claude")
    monkeypatch.setenv("NYANPASU_CLAUDE_BIN", "/custom runtime/claude")
    monkeypatch.setenv("NYANPASU_CODEX_BIN", "/custom runtime/codex")
    (tmp_path / "config.toml").write_text(
        '[claude]\nargs=["--profile", "team"]\n[codex]\nargs=["--profile", "review"]\n'
    )
    config = load_config()
    assert config.runtime.backend == "claude"
    assert config.claude.command == ("/custom runtime/claude", "--profile", "team")
    assert config.codex.command == ("/custom runtime/codex", "--profile", "review")
    for cls in (CodexConfig, ClaudeConfig):
        monkeypatch.chdir(tmp_path)
        assert cls(bin="./wrapper").bin == str(tmp_path / "wrapper")
        with pytest.raises(ValueError):
            cls(bin="")
        with pytest.raises(ValueError):
            cls(args=("bad\0",))


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
