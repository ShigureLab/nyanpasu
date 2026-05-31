from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_HOME = Path("~/.nyanpasu")
CONFIG_FILE_NAME = "config.toml"


class CodexConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    backend: Literal["app-server", "exec"] = "app-server"
    bin: str = "codex"
    model: str | None = None
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    approval_policy: Literal["untrusted", "on-failure", "on-request", "never"] = "never"
    command_timeout_seconds: int = 60 * 60
    pass_env: tuple[str, ...] = ()

    @field_validator("pass_env", mode="before")
    @classmethod
    def _pass_env_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_str_tuple(value)


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "127.0.0.1"
    port: int = 8765


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    concurrency: int = 4
    coalesce_window_seconds: int = 600
    context_lease_seconds: float = 2 * 60 * 60
    context_lease_heartbeat_seconds: float = 30
    context_lease_wait_seconds: float = 5
    clean_event_snapshots: bool = True


class NyanpasuConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    state_dir: Path = Field(default_factory=lambda: nyanpasu_home())
    server: ServerConfig = Field(default_factory=ServerConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    enabled_plugins: tuple[str, ...] = ()

    @field_validator("state_dir", mode="before")
    @classmethod
    def _state_dir_path(cls, value: Any) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("enabled_plugins", mode="before")
    @classmethod
    def _enabled_plugins_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_str_tuple(value)

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"


def load_config() -> NyanpasuConfig:
    raw: dict[str, Any] = {}
    config_path = default_config_path()
    if config_path.exists():
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("config root must be a TOML table")
        if "state_dir" in parsed:
            raise ValueError("state_dir is not configurable; set NYANPASU_HOME to choose the Nyanpasu home directory")
        raw = parsed

    normalized = _normalize_legacy_flat_config(raw)
    normalized = _merge_env(normalized)
    return NyanpasuConfig.model_validate(normalized)


def nyanpasu_home() -> Path:
    return Path(os.getenv("NYANPASU_HOME", str(DEFAULT_HOME))).expanduser().resolve()


def default_config_path() -> Path:
    return nyanpasu_home() / CONFIG_FILE_NAME


def ensure_state_dirs(config: NyanpasuConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.worktrees_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)


def _normalize_legacy_flat_config(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    codex = dict(data.get("codex") or {})
    server = dict(data.get("server") or {})
    runtime = dict(data.get("runtime") or {})
    for old, new in {
        "codex_backend": "backend",
        "codex_bin": "bin",
        "model": "model",
        "sandbox": "sandbox",
        "approval_policy": "approval_policy",
        "command_timeout_seconds": "command_timeout_seconds",
        "pass_env": "pass_env",
    }.items():
        if old in data and new not in codex:
            codex[new] = data.pop(old)
    for key in ("host", "port"):
        if key in data and key not in server:
            server[key] = data.pop(key)
    for old, new in {
        "concurrency": "concurrency",
        "poll_interval_seconds": "coalesce_window_seconds",
        "clean_event_worktrees": "clean_event_snapshots",
    }.items():
        if old in data and new not in runtime:
            runtime[new] = data.pop(old)
        if old in runtime and new not in runtime:
            runtime[new] = runtime.pop(old)
    if codex:
        data["codex"] = codex
    if server:
        data["server"] = server
    if runtime:
        data["runtime"] = runtime
    return data


def _merge_env(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    data.setdefault("state_dir", nyanpasu_home())
    server = dict(data.get("server") or {})
    if host := os.getenv("NYANPASU_HOST"):
        server["host"] = host
    if port := os.getenv("NYANPASU_PORT"):
        server["port"] = int(port)
    if server:
        data["server"] = server
    codex = dict(data.get("codex") or {})
    env_codex = {
        "NYANPASU_CODEX_BACKEND": "backend",
        "NYANPASU_CODEX_BIN": "bin",
        "NYANPASU_CODEX_MODEL": "model",
        "NYANPASU_CODEX_SANDBOX": "sandbox",
        "NYANPASU_CODEX_APPROVAL": "approval_policy",
        "NYANPASU_COMMAND_TIMEOUT_SECONDS": "command_timeout_seconds",
    }
    for env_name, field in env_codex.items():
        value = os.getenv(env_name)
        if value is not None:
            codex[field] = int(value) if field == "command_timeout_seconds" else value
    if codex:
        data["codex"] = codex
    if plugins := os.getenv("NYANPASU_PLUGINS"):
        data["enabled_plugins"] = plugins
    return data


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    raise ValueError("expected a string or list of strings")
