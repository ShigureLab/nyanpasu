from __future__ import annotations

from pathlib import Path

import pytest

from nyanpasu.claude import ClaudeBackend
from nyanpasu.codex import CodexAppServerBackend
from nyanpasu.config import (
    ClaudeBackendConfig,
    CodexBackendConfig,
    NyanpasuConfig,
    ProcessConfig,
    TaskPolicy,
    TasksConfig,
)
from nyanpasu.native_home import native_env, seed_home
from nyanpasu.targets import ExecutionOverride


def test_seed_home_copies_only_named_configuration_and_authentication(tmp_path):
    original = tmp_path / "operator-home"
    codex = original / ".codex"
    claude = original / ".claude"
    for directory in (codex, claude):
        directory.mkdir(parents=True)
        (directory / "memory.md").write_text("private operator memory")
        (directory / "sessions").mkdir()
        (directory / "sessions" / "private.jsonl").write_text("private operator history")
    (codex / "config.toml").write_text('model_provider = "configured-provider"\n')
    (codex / "auth.json").write_text('{"credential":"model-service-credential"}')
    (claude / "settings.json").write_text('{"model":"configured-model"}')
    (claude / ".credentials.json").write_text('{"credential":"another-model-service-credential"}')
    (original / ".claude.json").write_text('{"projects":{"private":"must not copy"}}')
    home = tmp_path / "context-home"
    env = {"HOME": str(original)}
    assert seed_home(home, "codex", env) == home / ".codex"
    assert seed_home(home, "claude-code", env) == home / ".claude"
    assert {path.name for path in (home / ".codex").iterdir()} == {"auth.json", "config.toml"}
    assert {path.name for path in (home / ".claude").iterdir()} == {"settings.json", ".credentials.json"}
    assert not (home / ".claude.json").exists()
    assert (home / ".codex" / "auth.json").stat().st_mode & 0o777 == 0o600
    (home / ".codex" / "auth.json").write_text("refreshed context credentials")
    seed_home(home, "codex", env)
    assert (home / ".codex" / "auth.json").read_text() == "refreshed context credentials"


def test_seed_home_honors_explicit_config_locations(tmp_path):
    configured = tmp_path / "provider-config"
    configured.mkdir()
    (configured / "settings.json").write_text("{}")
    native = seed_home(tmp_path / "isolated", "claude-code", {"CLAUDE_CONFIG_DIR": str(configured)})
    assert (native / "settings.json").read_text() == "{}"


def test_explicit_home_template_supports_custom_native_directory_and_retains_refreshes(tmp_path):
    template = tmp_path / "minimal-template"
    relative = Path(".cc-mirror/codewiz-cc/config")
    (template / relative).mkdir(parents=True)
    (template / relative / "settings.json").write_text('{"provider":"codewiz"}')
    (template / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
    operator = tmp_path / "operator"
    operator.mkdir()
    (operator / "settings.json").write_text('{"operator":"must not use"}')
    (operator / "history.jsonl").write_text("private history")
    home = tmp_path / "home"
    native = seed_home(
        home, "claude-code", {"CLAUDE_CONFIG_DIR": str(operator)}, native_directory=relative, template=template
    )
    assert native == home / relative
    assert (native / "settings.json").read_text() == '{"provider":"codewiz"}'
    assert (home / ".claude.json").read_text() == '{"hasCompletedOnboarding":true}'
    assert not (native / "history.jsonl").exists()
    assert native.stat().st_mode & 0o777 == 0o700
    assert (native / "settings.json").stat().st_mode & 0o777 == 0o600
    (native / "settings.json").write_text("refreshed")
    seed_home(home, "claude-code", {}, native_directory=relative, template=template)
    assert (native / "settings.json").read_text() == "refreshed"


@pytest.mark.parametrize("position", ["template-root", "template-file", "target-parent", "target-file"])
def test_home_template_rejects_source_and_target_symlinks(tmp_path, position):
    template = tmp_path / "template"
    (template / "config").mkdir(parents=True)
    (template / "config" / "settings.json").write_text("new settings")
    private = tmp_path / "private"
    private.mkdir()
    secret = private / "settings.json"
    secret.write_text("secret")
    home = tmp_path / "home"
    home.mkdir()
    if position == "template-root":
        alias = tmp_path / "template-link"
        alias.symlink_to(template, target_is_directory=True)
        template = alias
    elif position == "template-file":
        (template / "config" / "settings.json").unlink()
        (template / "config" / "settings.json").symlink_to(secret)
    elif position == "target-parent":
        (home / "config").symlink_to(private, target_is_directory=True)
    else:
        (home / "config").mkdir()
        (home / "config" / "settings.json").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        seed_home(home, "claude-code", {}, native_directory=Path("config"), template=template)
    assert secret.read_text() == "secret"


@pytest.mark.parametrize("relative", [Path("/operator/.codex"), Path("../operator"), Path()])
def test_native_directory_cannot_escape_home(tmp_path, relative):
    with pytest.raises(ValueError, match="native_directory"):
        seed_home(tmp_path / "home", "codex", {}, native_directory=relative)


def test_context_environment_preserves_only_native_directories_inside_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    custom = home / ".cc-mirror" / "codewiz-cc" / "config"
    env = native_env({"CLAUDE_CONFIG_DIR": str(custom), "CODEX_HOME": "/operator/codex"}, home=home)
    assert env["CLAUDE_CONFIG_DIR"] == str(custom)
    assert env["CODEX_HOME"] == str(home / ".codex")
    (home / "escape").symlink_to(tmp_path / "other-home", target_is_directory=True)
    env = native_env({"CODEX_HOME": str(home / "escape" / "native")}, home=home)
    assert env["CODEX_HOME"] == str(home / ".codex")


@pytest.mark.parametrize("link_directory", [True, False])
def test_backend_symlinks_cannot_redirect_service_seeding(tmp_path, link_directory):
    source = tmp_path / "operator"
    source.mkdir()
    (source / "auth.json").write_text("secret auth")
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if link_directory:
        (home / ".codex").symlink_to(outside, target_is_directory=True)
    else:
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").symlink_to(outside / "auth.json")
    with pytest.raises(ValueError, match="symlink|directory"):
        seed_home(home, "codex", {"CODEX_HOME": str(source)})
    assert not (outside / "auth.json").exists()


def test_context_environment_rewrites_native_state_and_drops_service_configuration(tmp_path):
    home = tmp_path / "contexts" / "one"
    env = native_env(
        {
            "HOME": "/operator",
            "CODEX_HOME": "/operator/codex",
            "CLAUDE_CONFIG_DIR": "/operator/claude",
            "XDG_DATA_HOME": "/operator/data",
            "TMPDIR": str(tmp_path / "system-temp"),
            "NYANPASU_TOKEN": "service-admin-secret",
            "NYANPASU_HOME": "/service",
            "MODEL_API_KEY": "explicit-backend-credential",
        },
        home=home,
    )
    assert env["HOME"] == str(home)
    assert env["CODEX_HOME"] == str(home / ".codex")
    assert env["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["TMPDIR"] == str(tmp_path / "system-temp")
    assert "NYANPASU_TOKEN" not in env and "NYANPASU_HOME" not in env
    assert env["MODEL_API_KEY"] == "explicit-backend-credential"


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
@pytest.mark.parametrize("source", ["pass_env", "env"])
def test_explicit_backend_credentials_survive_without_service_configuration(tmp_path, monkeypatch, backend, source):
    credential = {"NYANPASU_GITHUB_TOKEN": "agent-credential"}
    administrative = {
        "NYANPASU_HOME": "/operator/service",
        "NYANPASU_TOKEN": "old-admin-token",
        "NYANPASU__SERVER__TOKEN": "admin-token",
        "NYANPASU__BACKENDS__CODEX__PROCESS__ENV__SECRET": "service-config",
    }
    for key, value in {**credential, **administrative, "NYANPASU_NOT_GRANTED": "not-authorized"}.items():
        monkeypatch.setenv(key, value)
    granted = {**credential, **administrative}
    configured = (
        ProcessConfig(command=("true",), pass_env=tuple(granted))
        if source == "pass_env"
        else ProcessConfig(command=("true",), env=granted)
    )
    config = NyanpasuConfig(
        state_dir=tmp_path / "state",
        backends={
            "worker": CodexBackendConfig(process=configured)
            if backend == "codex"
            else ClaudeBackendConfig(process=configured)
        },
        tasks=TasksConfig(defaults=TaskPolicy(execution=ExecutionOverride(backend="worker"))),
    )
    home = tmp_path / "context-home"
    env = (
        CodexAppServerBackend(config, "worker", home=home)._env
        if backend == "codex"
        else ClaudeBackend(config, "worker", home=home).env
    )
    assert env["NYANPASU_GITHUB_TOKEN"] == "agent-credential"
    assert not set(administrative).intersection(env)
    assert "NYANPASU_NOT_GRANTED" not in env
    assert env["HOME"] == str(home)
    assert env["CODEX_HOME"] == str(home / ".codex")
    assert env["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
