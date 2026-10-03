from __future__ import annotations

import copy
import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from nyanpasu.targets import ExecutionOverride, ExecutionTarget

DEFAULT_HOME = Path("~/.nyanpasu")
CONFIG_FILE_NAME = "config.toml"
CODEX_COMMAND = ("codex",)
CLAUDE_COMMAND = ("claude", "--permission-prompts", "none", "--system-prompt-snapshot", "off")


class EnvCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cmd: tuple[str, ...] = Field(min_length=1, repr=False)

    @field_validator("cmd")
    @classmethod
    def _valid_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value[0] or any("\0" in arg for arg in value):
            raise ValueError("cmd requires a nonempty executable and arguments without NUL")
        return value


class ModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    model: str | None = None
    reasoning: str | None = None

    _settings = field_validator("model", "reasoning")(ExecutionOverride._nonempty.__func__)


class FallbackModel(ModelSettings):
    model: str

    @field_validator("model")
    @classmethod
    def _single_model(cls, value: str) -> str:
        if "," in value:
            raise ValueError("fallback model must contain no comma")
        return value


class ProcessConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    command: tuple[str, ...] = Field(min_length=1)
    pass_env: tuple[str, ...] = ()
    env: dict[str, str | EnvCommand] = Field(default_factory=dict, repr=False)

    @field_validator("command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value[0].strip() or any("\0" in arg for arg in value):
            raise ValueError("command requires a nonempty executable and arguments without NUL")
        executable = value[0]
        path = Path(executable).expanduser()
        # Preserve executable symlinks, notably virtual-environment interpreters.
        if os.sep in executable or (os.altsep and os.altsep in executable):
            executable = str(path.absolute())
        return (executable, *value[1:])

    @field_validator("env")
    @classmethod
    def _valid_env(cls, value: dict[str, str | EnvCommand]) -> dict[str, str | EnvCommand]:
        for key, source in value.items():
            if not key or "=" in key or "\0" in key:
                raise ValueError("environment variable names must be nonempty and contain neither '=' nor NUL")
            if isinstance(source, str) and "\0" in source:
                raise ValueError("environment variable values must not contain NUL")
        return value


class CodexOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    approval_policy: Literal["untrusted", "on-request", "never"] = "on-request"
    approvals_reviewer: Literal["user", "auto_review"] = "auto_review"
    rpc_timeout_seconds: int = Field(default=60, gt=0)


class ClaudeOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    permission_mode: Literal["default", "manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"] = (
        "auto"
    )
    allowed_tools: tuple[str, ...] = ()
    fallback_models: tuple[FallbackModel, ...] = Field(default=(), max_length=3)

    @field_validator("fallback_models", mode="before")
    @classmethod
    def _fallback_models(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return tuple({"model": item} if isinstance(item, str) else item for item in value)
        return value

    @field_validator("fallback_models")
    @classmethod
    def _fallback_reasoning(cls, value: tuple[FallbackModel, ...]) -> tuple[FallbackModel, ...]:
        if any(item.reasoning not in {None, "low", "medium", "high", "xhigh"} for item in value):
            raise ValueError("Claude per-model fallback reasoning supports low, medium, high, and xhigh")
        return value


class NativeHomeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    native_directory: Path
    template: Path | None = None

    @field_validator("native_directory")
    @classmethod
    def _relative_directory(cls, value: Path) -> Path:
        if value.is_absolute() or not value.parts or ".." in value.parts:
            raise ValueError("native_directory must be a nonempty path within the context home")
        return value


class CodexBackendConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    driver: Literal["codex"] = "codex"
    home: NativeHomeConfig = Field(default_factory=lambda: NativeHomeConfig(native_directory=Path(".codex")))
    process: ProcessConfig = Field(default_factory=lambda: ProcessConfig(command=CODEX_COMMAND))
    defaults: ModelSettings = Field(default_factory=ModelSettings)
    options: CodexOptions = Field(default_factory=CodexOptions)

    @field_validator("process", mode="before")
    @classmethod
    def _process_defaults(cls, value: Any) -> Any:
        return {"command": CODEX_COMMAND, **value} if isinstance(value, dict) else value


class ClaudeBackendConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    driver: Literal["claude-code"] = "claude-code"
    home: NativeHomeConfig = Field(default_factory=lambda: NativeHomeConfig(native_directory=Path(".claude")))
    process: ProcessConfig = Field(default_factory=lambda: ProcessConfig(command=CLAUDE_COMMAND))
    defaults: ModelSettings = Field(default_factory=ModelSettings)
    options: ClaudeOptions = Field(default_factory=ClaudeOptions)

    @field_validator("process", mode="before")
    @classmethod
    def _process_defaults(cls, value: Any) -> Any:
        return {"command": CLAUDE_COMMAND, **value} if isinstance(value, dict) else value


BackendConfig = Annotated[CodexBackendConfig | ClaudeBackendConfig, Field(discriminator="driver")]


def default_backends() -> dict[str, BackendConfig]:
    return {"codex": CodexBackendConfig(), "claude": ClaudeBackendConfig()}


class TaskLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    turn_timeout_seconds: int | None = Field(default=None, gt=0)


class TaskPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution: ExecutionOverride = Field(default_factory=ExecutionOverride)
    limits: TaskLimits = Field(default_factory=TaskLimits)


class TasksConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    defaults: TaskPolicy = Field(
        default_factory=lambda: TaskPolicy(
            execution=ExecutionOverride(backend="codex"), limits=TaskLimits(turn_timeout_seconds=3600)
        )
    )
    kinds: dict[str, TaskPolicy] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: tuple[str, ...] = ()
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)


class MemoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    consolidate: bool = True
    max_results_per_search: int = Field(default=10, ge=1, le=100)


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    host: str = "127.0.0.1"
    port: int = 8765
    token: SecretStr | None = Field(default=None, repr=False)

    @field_validator("token")
    @classmethod
    def _token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value.get_secret_value()):
            raise ValueError("server token must be a nonempty bearer token without whitespace")
        return value


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    concurrency: int = 4
    coalesce_window_seconds: int = 600
    context_lease_seconds: float = 2 * 60 * 60
    context_lease_heartbeat_seconds: float = 30
    context_lease_wait_seconds: float = 5
    clean_event_snapshots: bool = True


class NyanpasuConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    state_dir: Path = Field(default_factory=lambda: nyanpasu_home())
    server: ServerConfig = Field(default_factory=ServerConfig)
    backends: dict[str, BackendConfig] = Field(default_factory=default_backends)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    integrations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)

    @property
    def enabled_plugin_ids(self) -> tuple[str, ...]:
        return self.plugins.enabled

    @property
    def memory_dir(self) -> Path:
        return self.state_dir / "memory"

    @model_validator(mode="after")
    def _execution_policies(self) -> NyanpasuConfig:
        for name in self.backends:
            if not name.strip():
                raise ValueError("backend names must be nonempty")
        self.resolve_execution()
        for kind in self.tasks.kinds:
            self.resolve_execution(kind)
        return self

    def resolve_execution(self, kind: str = "default", override: ExecutionOverride | None = None) -> ExecutionTarget:
        policy = self.tasks.kinds.get(kind)
        layers = [("tasks.defaults.execution", self.tasks.defaults.execution)]
        if policy is not None:
            layers.append((f"tasks.kinds.{kind}.execution", policy.execution))
        if override is not None:
            layers.append(("request.execution", override))
        selected = "codex"
        backend_source = "builtin"
        annotated: list[tuple[str, str, ExecutionOverride]] = []
        for source, layer in layers:
            if layer.backend is not None:
                selected = layer.backend
                backend_source = source
            if selected not in self.backends:
                raise ValueError(f"unknown execution backend: {selected}")
            annotated.append((selected, source, layer))
        backend = self.backends[selected]
        settings: dict[str, str | None] = {}
        sources = {"backend": backend_source}
        for field in ("model", "reasoning"):
            value = getattr(backend.defaults, field)
            sources[field] = f"backends.{selected}.defaults" if value is not None else "native"
            for owner, source, layer in annotated:
                if owner == selected and (candidate := getattr(layer, field)) is not None:
                    value = candidate
                    sources[field] = source
            settings[field] = value
        timeout = self.tasks.defaults.limits.turn_timeout_seconds or 3600
        sources["turn_timeout_seconds"] = "tasks.defaults.limits"
        if policy is not None and policy.limits.turn_timeout_seconds is not None:
            timeout = policy.limits.turn_timeout_seconds
            sources["turn_timeout_seconds"] = f"tasks.kinds.{kind}.limits"
        return ExecutionTarget(
            backend=selected,
            driver=backend.driver,
            model=settings["model"],
            reasoning=settings["reasoning"],
            turn_timeout_seconds=timeout,
            sources=sources,
        )

    @field_validator("state_dir", mode="before")
    @classmethod
    def _state_dir_path(cls, value: Any) -> Path:
        return Path(value).expanduser().resolve()

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

    normalized = _merge_env(raw)
    return NyanpasuConfig.model_validate(normalized)


def nyanpasu_home() -> Path:
    return Path(os.getenv("NYANPASU_HOME", str(DEFAULT_HOME))).expanduser().resolve()


def default_config_path() -> Path:
    return nyanpasu_home() / CONFIG_FILE_NAME


def ensure_state_dirs(config: NyanpasuConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.worktrees_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)


def _merge_env(raw: dict[str, Any]) -> dict[str, Any]:
    """Use one nested namespace; each value is TOML or an unquoted string."""
    legacy = {
        "NYANPASU_HOST",
        "NYANPASU_PORT",
        "NYANPASU_TOKEN",
        "NYANPASU_BACKEND",
        "NYANPASU_PLUGINS",
        "NYANPASU_COMMAND_TIMEOUT_SECONDS",
        "NYANPASU_CODEX_BIN",
        "NYANPASU_CODEX_MODEL",
        "NYANPASU_CODEX_REASONING_EFFORT",
        "NYANPASU_CODEX_SANDBOX",
        "NYANPASU_CODEX_APPROVAL",
        "NYANPASU_CODEX_APPROVALS_REVIEWER",
        "NYANPASU_CLAUDE_BIN",
        "NYANPASU_CLAUDE_MODEL",
        "NYANPASU_CLAUDE_REASONING_EFFORT",
        "NYANPASU_CLAUDE_PERMISSION_MODE",
        "NYANPASU_CLAUDE_FALLBACK_MODELS",
    }
    if obsolete := sorted(legacy.intersection(os.environ)):
        raise ValueError(
            "legacy configuration environment keys require migration to NYANPASU__: " + ", ".join(obsolete)
        )
    data = copy.deepcopy(raw)
    data.setdefault("state_dir", nyanpasu_home())
    for name, value in os.environ.items():
        if not name.startswith("NYANPASU__"):
            continue
        parts = name.removeprefix("NYANPASU__").split("__")
        path = [part.lower() for part in parts]
        if len(path) >= 5 and path[0] == "backends" and path[2:4] == ["process", "env"]:
            path[4] = parts[4]  # Child process environment names are case-sensitive.
        if any(not part for part in path) or path[0] == "state_dir":
            raise ValueError(f"invalid configuration environment key: {name}")
        try:
            parsed = tomllib.loads(f"value = {value}")["value"]
        except tomllib.TOMLDecodeError:
            parsed = value
        if path[0] == "backends" and "backends" not in data:
            data["backends"] = {name: {"driver": backend.driver} for name, backend in default_backends().items()}
        target = data
        for part in path[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError(f"configuration environment path crosses a value: {name}")
        target[path[-1]] = parsed
    return data
