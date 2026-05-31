from __future__ import annotations

from typing import TYPE_CHECKING

from nyanpasu.config import default_config_path, load_config, nyanpasu_home

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

[runtime]
concurrency = 2
coalesce_window_seconds = 60
clean_event_snapshots = false

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
    assert config.runtime.concurrency == 2
    assert config.runtime.coalesce_window_seconds == 60
    assert config.runtime.clean_event_snapshots is False
    assert config.enabled_plugins == ("github_reviewer",)
    assert config.plugins["github_reviewer"]["github_login"] == "review-bot"


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


def test_load_config_accepts_legacy_clean_event_worktrees(tmp_path: Path, monkeypatch) -> None:
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

    config = load_config()

    assert config.runtime.clean_event_snapshots is False


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
