from __future__ import annotations

import pytest

from nyanpasu.config import (
    ClaudeOptions,
    EnvCommand,
    ModelSettings,
    NyanpasuConfig,
    ProcessConfig,
    ServerConfig,
    load_config,
)
from nyanpasu.targets import ExecutionOverride


def test_server_token_from_config_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[server]\ntoken = "file-secret"\n')
    config = load_config()
    assert config.server.token.get_secret_value() == "file-secret"
    assert "file-secret" not in repr(config) and "file-secret" not in config.model_dump_json()
    monkeypatch.setenv("NYANPASU__SERVER__TOKEN", "env-secret")
    assert load_config().server.token.get_secret_value() == "env-secret"
    monkeypatch.setenv("NYANPASU__SERVER__TOKEN", "")
    with pytest.raises(ValueError, match="nonempty bearer token"):
        load_config()


@pytest.mark.parametrize("token", ["", " ", "secret token", "secret\n", "secret\0", "秘密", 123])
def test_server_token_rejects_invalid_values_without_disclosing_them(token):
    with pytest.raises(ValueError) as error:
        ServerConfig(token=token)
    assert "secret" not in str(error.value)


def test_load_config_and_nested_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("""
[backends.codex]
driver = "codex"
[backends.codex.defaults]
model = "configured-model"
reasoning = "medium"
[backends.codex.process]
command = ["codex", "--profile", "worker"]
pass_env = ["GH_TOKEN"]
[backends.codex.process.env]
GH_TOKEN = {cmd = ["missing-command", "--user", "review-bot"]}
[runtime]
concurrency = 2
[plugins]
enabled = ["github_reviewer"]
[plugins.settings.github_reviewer]
github_login = "review-bot"
""")
    config = load_config()
    assert config.backends["codex"].process.env == {
        "GH_TOKEN": EnvCommand(cmd=("missing-command", "--user", "review-bot"))
    }
    assert config.resolve_execution().model == "configured-model"
    assert config.enabled_plugin_ids == ("github_reviewer",)
    assert config.plugins.settings["github_reviewer"]["github_login"] == "review-bot"
    monkeypatch.setenv("NYANPASU__BACKENDS__CODEX__DEFAULTS__MODEL", "env-model")
    monkeypatch.setenv("NYANPASU__RUNTIME__CONCURRENCY", "7")
    monkeypatch.setenv("NYANPASU__PLUGINS__ENABLED", "[]")
    config = load_config()
    assert config.resolve_execution().model == "env-model"
    assert config.resolve_execution().reasoning == "medium"
    assert config.runtime.concurrency == 7
    assert config.enabled_plugin_ids == ()


@pytest.fixture
def routed():
    return NyanpasuConfig.model_validate(
        {
            "backends": {
                "codex": {"driver": "codex", "defaults": {"model": "codex-native", "reasoning": "high"}},
                "claude": {"driver": "claude-code", "defaults": {"model": "claude-native", "reasoning": "medium"}},
                "fast": {"driver": "codex", "defaults": {"model": "fast-native"}},
            },
            "tasks": {
                "defaults": {
                    "execution": {"backend": "codex", "model": "codex-global"},
                    "limits": {"turn_timeout_seconds": 123},
                },
                "kinds": {
                    "review": {
                        "execution": {"backend": "claude", "model": "claude-review"},
                        "limits": {"turn_timeout_seconds": 456},
                    },
                    "code": {"execution": {"model": "codex-code", "reasoning": "ultra"}},
                },
            },
        }
    )


def test_resolution_keeps_model_and_reasoning_with_their_backend(routed):
    default = routed.resolve_execution()
    assert (default.backend, default.model, default.reasoning, default.turn_timeout_seconds) == (
        "codex",
        "codex-global",
        "high",
        123,
    )
    review = routed.resolve_execution("review")
    assert (review.backend, review.model, review.reasoning, review.turn_timeout_seconds) == (
        "claude",
        "claude-review",
        "medium",
        456,
    )
    switched_back = routed.resolve_execution("review", ExecutionOverride(backend="codex"))
    assert (switched_back.model, switched_back.reasoning) == ("codex-global", "high")
    assert switched_back.sources["model"] == "tasks.defaults.execution"
    fast = routed.resolve_execution("code", ExecutionOverride(backend="fast"))
    assert (fast.driver, fast.model, fast.reasoning) == ("codex", "fast-native", None)
    assert fast.sources["reasoning"] == "native"


def test_same_backend_overrides_inherit_remaining_fields(routed):
    code = routed.resolve_execution("code", ExecutionOverride(model="request-model"))
    assert (code.backend, code.model, code.reasoning) == ("codex", "request-model", "ultra")
    assert code.sources["model"] == "request.execution"
    assert code.sources["reasoning"] == "tasks.kinds.code.execution"
    with pytest.raises(ValueError, match="unknown execution backend"):
        routed.resolve_execution("review", ExecutionOverride(backend="missing"))


def test_named_backend_configuration_rejects_unknown_driver_and_routing():
    with pytest.raises(ValueError, match="driver"):
        NyanpasuConfig.model_validate({"backends": {"codex": {"driver": "unknown"}}})
    with pytest.raises(ValueError, match="unknown execution backend"):
        NyanpasuConfig.model_validate({"tasks": {"kinds": {"review": {"execution": {"backend": "missing"}}}}})


@pytest.mark.parametrize(
    "models",
    [
        ("",),
        ("with,comma",),
        ({"model": "backup", "reasoning": ""},),
        ({"model": "backup", "reasoning": "max"},),
        ("a", "b", "c", "d"),
    ],
)
def test_claude_fallback_models_reject_invalid_settings(models):
    with pytest.raises(ValueError):
        ClaudeOptions(fallback_models=models)


@pytest.mark.parametrize("field", ["model", "reasoning"])
@pytest.mark.parametrize("value", ["", "  ", "value\0"])
def test_model_settings_reject_empty_or_invalid_values(field, value):
    with pytest.raises(ValueError, match="execution settings"):
        ModelSettings.model_validate({field: value})


@pytest.mark.parametrize(
    "env",
    [
        {"TOKEN": 123},
        {"TOKEN": {"cmd": "echo secret"}},
        {"TOKEN": {"cmd": []}},
        {"TOKEN": {"cmd": [""]}},
        {"TOKEN": {"cmd": ["echo", 123]}},
        {"TOKEN": {"cmd": ["echo", "secret\0"]}},
        {"TOKEN": {"cmd": ["echo", "secret"], "shell": True}},
        {"TOKEN": {"value": "secret"}},
        {"": "secret"},
        {"BAD=NAME": "secret"},
        {"BAD\0NAME": "secret"},
        {"TOKEN": "secret\0"},
    ],
)
def test_process_env_rejects_invalid_sources_without_showing_values(env):
    with pytest.raises(ValueError) as error:
        ProcessConfig(command=("codex",), env=env)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "legacy",
    [
        '[codex]\nmodel="old"',
        '[claude]\nmodel="old"',
        "enabled_plugins=[]",
        '[runtime]\nbackend="claude"',
        "[runtime]\nclean_event_worktrees=false",
        'approval_policy="on-request"',
        '[plugins.github_reviewer]\nagent_name="old"',
    ],
)
def test_loader_rejects_legacy_configuration(tmp_path, monkeypatch, legacy):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(legacy)
    with pytest.raises(ValueError, match="Extra inputs"):
        load_config()


def test_state_dir_is_environment_only_and_memory_path_is_derived(tmp_path, monkeypatch):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    config = load_config()
    assert config.memory_dir == tmp_path / "memory"
    assert config.memory.enabled and config.memory.consolidate
    (tmp_path / "config.toml").write_text('state_dir = "/tmp/other"')
    with pytest.raises(ValueError, match="state_dir is not configurable"):
        load_config()


def test_legacy_token_override_is_rejected_instead_of_disabling_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    monkeypatch.setenv("NYANPASU_TOKEN", "private-token")
    with pytest.raises(ValueError, match="legacy configuration environment keys") as error:
        load_config()
    assert "private-token" not in str(error.value)


def test_environment_can_override_builtin_backend_without_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path))
    monkeypatch.setenv("NYANPASU__BACKENDS__CODEX__DEFAULTS__MODEL", "env-model")
    assert load_config().resolve_execution().model == "env-model"


def test_explicit_migration_preserves_nested_plugin_settings_and_active_execution(tmp_path):
    import importlib.util
    import tomllib
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "migrate_config", Path(__file__).parents[1] / "scripts/migrate-config.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    raw = {
        "runtime": {"backend": "claude", "concurrency": 3},
        "claude": {
            "bin": "/opt/claude wrapper",
            "args": [],
            "model": "primary",
            "reasoning_effort": "high",
            "fallback_models": [{"model": "backup", "reasoning_effort": "medium"}],
            "command_timeout_seconds": 7200,
            "env": {"KEY": {"cmd": ["fetch-key"]}},
        },
        "plugins": {
            "github_reviewer": {
                "repos": {"owner/repo": {"base_branches": ["main"]}},
                "instruction_docs": [{"name": "SOUL.md", "path": "/tmp/soul"}],
            }
        },
    }
    data, notes = migration.migrate(raw)
    config = NyanpasuConfig.model_validate(tomllib.loads(migration.render_toml(data)))
    target = config.resolve_execution()
    assert (target.backend, target.driver, target.model, target.reasoning, target.turn_timeout_seconds) == (
        "claude",
        "claude-code",
        "primary",
        "high",
        7200,
    )
    assert config.backends["claude"].process.command == ("/opt/claude wrapper",)
    assert config.backends["claude"].options.fallback_models[0].reasoning == "medium"
    assert config.enabled_plugin_ids == ("github_reviewer",)
    assert config.plugins.settings == raw["plugins"]
    assert raw["runtime"]["backend"] == "claude"  # Input is not mutated.
    assert notes and "inactive codex timeout 3600" in notes[0]
    with pytest.raises(ValueError, match="already contains new"):
        migration.migrate(data)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("codex", {"backend": "codex"}),
        ("codex/model", {"backend": "codex", "model": "model"}),
        ("codex:high", {"backend": "codex", "reasoning": "high"}),
        ("codex/model:high", {"backend": "codex", "model": "model", "reasoning": "high"}),
    ],
)
def test_compact_execution_target(value, expected):
    assert ExecutionOverride.model_validate(value).model_dump(exclude_none=True) == expected


@pytest.mark.parametrize("value", ["", "codex/", "codex:", "/model", "codex/a/b", "codex/a:b:c"])
def test_compact_target_rejects_missing_and_ambiguous_fields(value):
    with pytest.raises(ValueError):
        ExecutionOverride.model_validate(value)
