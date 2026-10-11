from __future__ import annotations

from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, Mock

import pytest

from nyanpasu.backends import Backend, Backends
from nyanpasu.claude import ClaudeBackend
from nyanpasu.codex import CodexAppServerBackend
from nyanpasu.config import NyanpasuConfig
from nyanpasu.models import NativeSessionLocation


def test_named_registry_is_lazy_and_caches_by_backend_name(tmp_path):
    config = NyanpasuConfig.model_validate(
        {
            "state_dir": tmp_path,
            "backends": {
                "codex": {"driver": "codex"},
                "fast": {"driver": "codex", "defaults": {"model": "fast-model"}},
                "review": {"driver": "claude-code"},
            },
        }
    )
    backends = Backends(config)
    assert backends._instances == {}
    assert set(backends.runtime_info()["backends"]) == {"codex", "fast", "review"}
    assert backends.runtime_info()["backends"]["codex"]["resources"] == {
        "cpu_percent": 0,
        "rss_bytes": 0,
        "process_count": 0,
        "instances": [],
    }
    assert backends._instances == {}  # Dashboard reads do not launch or resolve credentials.
    first = backends.get("codex")
    fast = backends.get("fast")
    assert first is backends.get("codex") and first is not fast
    assert isinstance(fast.execution, CodexAppServerBackend)
    assert fast.execution.config.defaults.model == "fast-model"
    assert isinstance(backends.get("review").execution, ClaudeBackend)


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["codex", "claude"])
async def test_contexts_and_history_keep_their_own_native_home(tmp_path, name):
    template = tmp_path / "template"
    template.mkdir()
    config = NyanpasuConfig.model_validate(
        {
            "state_dir": tmp_path / "state",
            "backends": {
                "codex": {"driver": "codex", "home": {"template": template, "native_directory": ".codex"}},
                "claude": {
                    "driver": "claude-code",
                    "home": {"template": template, "native_directory": ".provider/config"},
                },
            },
        }
    )
    backends = Backends(config)
    field = "CODEX_HOME" if name == "codex" else "CLAUDE_CONFIG_DIR"
    for home in (tmp_path / "first-context", tmp_path / "second-context"):
        async with backends.turn(name, home=home) as backend:
            assert isinstance(backend, (CodexAppServerBackend, ClaudeBackend))
            env = backend._env if isinstance(backend, CodexAppServerBackend) else backend.env
            assert env["HOME"] == str(home)
            assert env[field] == str(home / config.backends[name].home.native_directory)
    historical_home = tmp_path / "historical-home"
    native = historical_home / "older-native-directory"
    backends.register_session_locator(
        lambda *_: NativeSessionLocation(
            native_home=native, isolated_home=historical_home, driver=config.backends[name].driver
        )
    )
    async with backends.history(name, "old-session") as old:
        assert isinstance(old.execution, (CodexAppServerBackend, ClaudeBackend))
        env = old.execution._env if isinstance(old.execution, CodexAppServerBackend) else old.execution.env
        assert env["HOME"] == str(historical_home)
        assert env[field] == str(native)


@pytest.mark.anyio
async def test_runtime_includes_concurrent_turns_and_history_and_clears_closed_processes(tmp_path, monkeypatch):
    config = NyanpasuConfig(state_dir=tmp_path, backends={"codex": {"driver": "codex"}})
    backends = Backends(config)
    executions = []

    def create(*args, **kwargs):
        pid = 100 + len(executions)
        execution = Mock(
            runtime_info=lambda: {"connection": "connected", "pids": [pid], "diagnostics": []},
            close=AsyncMock(),
        )
        executions.append(execution)
        return Backend(execution, Mock())

    monkeypatch.setattr(backends, "_create", create)
    sample = Mock(return_value={"codex": {"instances": [], "rss_bytes": 0}})
    monkeypatch.setattr(backends._resources, "sample", sample)
    backends.register_session_locator(
        lambda *_: NativeSessionLocation(native_home=tmp_path, isolated_home=tmp_path, driver="codex")
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(backends.turn("codex", home=tmp_path / "first"))
        await stack.enter_async_context(backends.turn("codex", home=tmp_path / "second"))
        await stack.enter_async_context(backends.history("codex", "thread"))
        observed = backends.runtime_info()["backends"]["codex"]
        assert observed["active_turns"] == 2
        assert len(backends._active) == 3
        assert observed["pids"] == [100, 101, 102]
        sample.assert_called_once_with({"codex": [100, 101, 102]})
        executions[-1].runtime_info = lambda: {"connection": "idle", "pids": [], "diagnostics": []}
        assert backends.runtime_info()["backends"]["codex"]["connection"] == "connected"
    assert all(execution.close.await_count == 1 for execution in executions)
    idle = backends.runtime_info()["backends"]["codex"]
    assert idle["active_turns"] == 0
    assert idle["pids"] == []
    assert idle["resources"]["instances"] == []
    assert idle["resources"]["rss_bytes"] == 0
