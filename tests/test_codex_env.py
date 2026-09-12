from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from nyanpasu.codex import CodexAppServerBackend, CodexExecBackend, safe_codex_env
from nyanpasu.config import CodexConfig, EnvCommand, NyanpasuConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_env_sources_override_inheritance_without_changing_command_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "original")
    monkeypatch.setenv("NOT_PASSED", "command-only")
    config = NyanpasuConfig(
        state_dir=tmp_path,
        codex=CodexConfig(
            pass_env=("GH_TOKEN",),
            env={
                "GH_TOKEN": "static-$NOT_PASSED",
                "EMPTY": "",
                "RESULT": EnvCommand(
                    cmd=(
                        sys.executable,
                        "-c",
                        "import os, sys; "
                        "sys.stdout.write(os.getcwd() + '|' + os.environ['GH_TOKEN'] + '|' "
                        "+ os.environ['NOT_PASSED'] + '|' + sys.argv[1] + '  \\r\\n')",
                        "$(echo expanded)",
                    )
                ),
            },
        ),
    )

    env = safe_codex_env(config)

    assert env["GH_TOKEN"] == "static-$NOT_PASSED"
    assert env["EMPTY"] == ""
    assert env["RESULT"] == f"{tmp_path}|original|command-only|$(echo expanded)  "
    assert "NOT_PASSED" not in env
    assert os.environ["GH_TOKEN"] == "original"
    assert "RESULT" not in os.environ
    assert "static-$NOT_PASSED" not in repr(config)


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("import sys; print('private-secret'); print('private-secret', file=sys.stderr); sys.exit(3)", "status 3"),
        ("print()", "nonempty"),
        ("print('\\0')", "NUL"),
        ("import sys; sys.stdout.buffer.write(b'\\xff')", "UTF-8"),
    ],
)
def test_command_failure_prevents_backend_creation(tmp_path: Path, monkeypatch, script: str, message: str) -> None:
    monkeypatch.setenv("GH_TOKEN", "inherited-secret")
    config = NyanpasuConfig(
        state_dir=tmp_path,
        codex=CodexConfig(pass_env=("GH_TOKEN",), env={"GH_TOKEN": EnvCommand(cmd=(sys.executable, "-c", script))}),
    )
    with pytest.raises(ValueError, match=f"codex.env.GH_TOKEN:.*{message}") as error:
        CodexExecBackend(config)
    assert "secret" not in str(error.value)


def test_missing_command_prevents_backend_creation(tmp_path: Path) -> None:
    config = NyanpasuConfig(
        state_dir=tmp_path, codex=CodexConfig(env={"GH_TOKEN": EnvCommand(cmd=(str(tmp_path / "missing"),))})
    )
    with pytest.raises(ValueError, match=r"codex.env.GH_TOKEN:.*FileNotFoundError"):
        CodexAppServerBackend(config)


def test_command_timeout_is_bounded_and_does_not_expose_output(tmp_path: Path, monkeypatch) -> None:
    run = subprocess.run

    def short_timeout(*args: Any, **kwargs: Any):
        assert kwargs["timeout"] == 10
        kwargs["timeout"] = 0.1
        return run(*args, **kwargs)

    monkeypatch.setattr("nyanpasu.codex.subprocess.run", short_timeout)
    config = NyanpasuConfig(
        state_dir=tmp_path,
        codex=CodexConfig(
            env={
                "GH_TOKEN": EnvCommand(
                    cmd=(sys.executable, "-c", "import time; print('private-secret', flush=True); time.sleep(60)")
                )
            }
        ),
    )
    with pytest.raises(ValueError, match=r"codex.env.GH_TOKEN: command timed out after 10 seconds") as error:
        CodexExecBackend(config)
    assert "private-secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("backend_class", [CodexExecBackend, CodexAppServerBackend])
def test_backend_reuses_environment_across_turns_restarts_and_cleanup(
    tmp_path: Path, monkeypatch, backend_class
) -> None:
    program = tmp_path / "codex"
    captured = tmp_path / "environments"
    counter = tmp_path / "resolutions"
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(captured)!r}, 'a') as out: out.write(os.environ['GH_TOKEN'] + '\\n')\n"
        "if sys.argv[1] == 'exec':\n"
        "    sys.stdin.read()\n"
        "    print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-1'}))\n"
        "else:\n"
        "    for line in sys.stdin:\n"
        "        request = json.loads(line)\n"
        "        if 'id' in request:\n"
        "            print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n",
        encoding="utf-8",
    )
    program.chmod(0o755)
    monkeypatch.setenv("GH_TOKEN", "initial")
    config = NyanpasuConfig(
        state_dir=tmp_path,
        codex=CodexConfig(
            bin=str(program),
            env={
                "GH_TOKEN": EnvCommand(
                    cmd=(
                        sys.executable,
                        "-c",
                        f"import os; open({str(counter)!r}, 'a').write('resolved\\n'); print(os.environ['GH_TOKEN'])",
                    )
                )
            },
        ),
    )
    backend = backend_class(config)
    monkeypatch.setenv("GH_TOKEN", "changed")

    async def run() -> None:
        try:
            if isinstance(backend, CodexExecBackend):
                await backend.run_turn(cwd=tmp_path, prompt="first", thread_id=None)
                await backend.run_turn(cwd=tmp_path, prompt="next", thread_id="thread-1")
                await backend.cleanup_thread("thread-1")
            else:
                await backend._ensure_started()
                await backend.close()
                await backend._ensure_started()
        finally:
            await backend.close()

    asyncio.run(run())

    assert counter.read_text().splitlines() == ["resolved"]
    assert captured.read_text().splitlines() == ["initial"] * (3 if backend_class is CodexExecBackend else 2)


def test_importing_cli_does_not_resolve_environment(tmp_path: Path) -> None:
    marker = tmp_path / "unexpected-command"
    (tmp_path / "config.toml").write_text(
        '[codex.env]\nGH_TOKEN = {cmd = ["touch", "' + str(marker) + '"]}\n', encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-c", "import nyanpasu.__main__"],
        env={**os.environ, "NYANPASU_HOME": str(tmp_path)},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert not marker.exists()
    assert not (tmp_path / "state.sqlite3").exists()
