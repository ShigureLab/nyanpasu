from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nyanpasu.config import CodexConfig, EnvCommand, default_config_path, load_config, nyanpasu_home

if TYPE_CHECKING:
    from pathlib import Path


def test_load_config_reads_home_config_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "home" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
enabled_plugins = ["github_reviewer"]

[server]
host = "0.0.0.0"
port = 9999

[codex]
backend = "exec"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
pass_env = ["GH_TOKEN"]

[codex.env]
TZ = "Asia/Shanghai"
GH_TOKEN = { cmd = ["missing-command", "--user", "review-bot"] }

[runtime]
concurrency = 2
coalesce_window_seconds = 60
clean_event_snapshots = false

[integrations.github]
token_env = "GH_TOKEN"
git_author_name = "Bot"
git_author_email = "bot@example.com"

[plugins.github_reviewer]
github_login = "review-bot"
poll_interval_seconds = 600
""".strip(),
        encoding="utf-8",
    )

    config = load_config()

    assert config.state_dir == (tmp_path / "home").resolve()
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9999
    assert config.codex.backend == "exec"
    assert config.codex.approval_policy == "on-request"
    assert config.codex.approvals_reviewer == "auto_review"
    assert config.codex.pass_env == ("GH_TOKEN",)
    assert config.codex.env == {
        "TZ": "Asia/Shanghai",
        "GH_TOKEN": EnvCommand(cmd=("missing-command", "--user", "review-bot")),
    }
    assert config.runtime.concurrency == 2
    assert config.runtime.coalesce_window_seconds == 60
    assert config.runtime.clean_event_snapshots is False
    assert config.integrations["github"]["token_env"] == "GH_TOKEN"
    assert config.integrations["github"]["git_author_name"] == "Bot"
    assert config.enabled_plugins == ("github_reviewer",)
    assert config.plugins["github_reviewer"]["github_login"] == "review-bot"


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
def test_codex_env_rejects_invalid_sources_without_showing_values(env) -> None:
    with pytest.raises(ValueError) as error:
        CodexConfig(env=env)
    assert "secret" not in str(error.value)


def test_load_config_allows_no_plugins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "home" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("", encoding="utf-8")

    config = load_config()

    assert config.plugins == {}
    assert config.enabled_plugins == ()


def test_load_config_uses_nyanpasu_home_config_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "home" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
[server]
port = 9998
""".strip(),
        encoding="utf-8",
    )

    config = load_config()

    assert nyanpasu_home() == (tmp_path / "home").resolve()
    assert default_config_path() == config_path.resolve()
    assert config.state_dir == (tmp_path / "home").resolve()
    assert config.server.port == 9998


def test_load_config_rejects_legacy_runtime_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "home" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
[runtime]
clean_event_worktrees = false
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="clean_event_worktrees"):
        load_config()


def test_load_config_rejects_legacy_flat_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "home" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('approval_policy = "on-request"', encoding="utf-8")

    with pytest.raises(ValueError, match="approval_policy"):
        load_config()


def test_load_config_rejects_state_dir_in_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "home" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('state_dir = "/tmp/other"', encoding="utf-8")

    try:
        load_config()
    except ValueError as exc:
        assert "state_dir is not configurable" in str(exc)
    else:
        raise AssertionError("expected state_dir to be rejected")
