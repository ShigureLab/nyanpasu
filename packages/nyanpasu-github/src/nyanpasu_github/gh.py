from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


class GitHubCommandError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = stderr.strip() or stdout.strip() or "no output"
        super().__init__(f"{' '.join(argv)} failed with exit {returncode}: {message}")


class GitHubSignatureError(ValueError):
    pass


def run_gh(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 60,
) -> subprocess.CompletedProcess[str]:
    argv = ["gh", *args]
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=_merged_env(env),
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise GitHubCommandError(argv, proc.returncode, proc.stdout, proc.stderr)
    return proc


def run_gh_with_env(env: Mapping[str, str] | None) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = 60,
    ) -> subprocess.CompletedProcess[str]:
        return run_gh(args, cwd=cwd, env=env, timeout=timeout)

    return runner


def gh_json(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 60,
) -> Any:
    proc = run_gh(args, cwd=cwd, env=env, timeout=timeout)
    return json.loads(proc.stdout or "null")


def resolve_branch_sha(repo: str, branch: str, *, env: Mapping[str, str] | None = None) -> str:
    data = gh_json(["api", f"repos/{repo}/git/ref/heads/{branch}"], env=env)
    if not isinstance(data, dict):
        raise ValueError("GitHub branch ref response must be an object")
    obj = data.get("object")
    if not isinstance(obj, dict) or not obj.get("sha"):
        raise ValueError(f"GitHub branch ref response did not contain a sha: {repo}@{branch}")
    return str(obj["sha"])


def verify_webhook_signature(body: bytes, signature: str | None, secret: str | None) -> None:
    if not secret:
        return
    if not signature or not signature.startswith("sha256="):
        raise GitHubSignatureError("missing GitHub signature")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    actual = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, actual):
        raise GitHubSignatureError("invalid GitHub signature")


def run_git(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 60,
) -> subprocess.CompletedProcess[str]:
    argv = ["git", *args]
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=_merged_env(env),
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise GitHubCommandError(argv, proc.returncode, proc.stdout, proc.stderr)
    return proc


def remote_name_for_url(local_path: Path, remote_url: str | None) -> str:
    if remote_url:
        proc = run_git(["remote", "-v"], cwd=local_path)
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == remote_url:
                return parts[0]
    return "origin"


def _merged_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    return os.environ | dict(env)
