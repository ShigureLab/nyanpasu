from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nyanpasu.config import EnvCommand, ProcessConfig


def resolve_env_value(source: str | EnvCommand, *, name: str, cwd: Path, env: Mapping[str, str]) -> str:
    if isinstance(source, str):
        return source
    try:
        result = subprocess.run(
            source.cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"{name}: command timed out after 10 seconds") from None
    except OSError as exc:
        raise ValueError(f"{name}: could not start command ({type(exc).__name__})") from None
    if result.returncode:
        raise ValueError(f"{name}: command exited with status {result.returncode}")
    try:
        value = result.stdout.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ValueError(f"{name}: command output is not UTF-8") from None
    if not value or "\0" in value:
        raise ValueError(f"{name}: command output must be nonempty and contain no NUL")
    return value


COMMON_ENV = frozenset(
    {
        "ALL_PROXY",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
BACKEND_ENV = {
    "codex": {"CODEX_HOME", "CODEX_NETWORK_ALLOW_LOCAL_BINDING", "CODEX_NETWORK_PROXY_ACTIVE"},
    "claude": {"CLAUDE_CONFIG_DIR"},
}


def process_env(config: ProcessConfig, *, cwd: Path, backend: str) -> dict[str, str]:
    inherited = dict(os.environ)
    allowed = COMMON_ENV | BACKEND_ENV[backend] | set(config.pass_env)
    env = {key: value for key in allowed if (value := inherited.get(key))}
    for key, source in config.env.items():
        env[key] = resolve_env_value(source, name=f"{backend}.env.{key}", cwd=cwd, env=inherited)
    # The CLI runs in task worktrees; history readers must use the same home.
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        if key in env:
            env[key] = str(Path(env[key]).expanduser().resolve())
    return env
