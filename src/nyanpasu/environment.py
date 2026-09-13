from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from nyanpasu.config import EnvCommand


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
