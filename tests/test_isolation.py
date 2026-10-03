from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from nyanpasu.isolation import ExecutionIsolation, seed_home


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


def test_wrap_rewrites_all_native_state_and_drops_service_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "contexts" / "one"
    _, env = ExecutionIsolation(home).wrap(
        ["python", "-c", "pass"],
        cwd=workspace,
        env={
            "HOME": "/operator",
            "CODEX_HOME": "/operator/codex",
            "CLAUDE_CONFIG_DIR": "/operator/claude",
            "XDG_DATA_HOME": "/operator/data",
            "NYANPASU_TOKEN": "service-admin-secret",
            "NYANPASU_HOME": "/service",
            "MODEL_API_KEY": "explicit-backend-credential",
        },
    )
    assert env["HOME"] == str(home)
    assert env["CODEX_HOME"] == str(home / ".codex")
    assert env["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["TMPDIR"] == "/tmp"
    assert "NYANPASU_TOKEN" not in env and "NYANPASU_HOME" not in env
    assert env["MODEL_API_KEY"] == "explicit-backend-credential"


def test_missing_isolator_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="bubblewrap"):
        ExecutionIsolation(tmp_path / "home").wrap(["true"], cwd=tmp_path / "workspace", env={})


def test_readonly_grants_reject_broad_home_and_service_parents(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    service = tmp_path / "service"
    workspace = service / "workspaces" / "current"
    workspace.mkdir(parents=True)
    home = service / "contexts" / "current"
    original = tmp_path / "operator"
    for forbidden in (Path("/"), Path("/proc"), original, original / ".codex", service):
        with pytest.raises(ValueError, match="readonly_paths"):
            ExecutionIsolation(home, readonly_paths=(forbidden,)).wrap(
                ["true"], cwd=workspace, env={"HOME": str(original)}
            )


def _run(isolation, workspace, script, *, env=None):
    if shutil.which(isolation.bwrap) is None:
        pytest.skip("bubblewrap is not installed on this test host")
    argv, isolated_env = isolation.wrap(
        ["/usr/bin/python3", "-c", script], cwd=workspace, env=env or {"PATH": os.defpath}
    )
    result = subprocess.run(argv, env=isolated_env, capture_output=True, text=True, timeout=20)
    if result.returncode and any(
        error in result.stderr
        for error in (
            "No permissions to create a new namespace",
            "Creating new namespace failed",
            "Can't mount proc",
        )
    ):
        pytest.skip(f"test host does not permit nested isolated proc: {result.stderr.strip()}")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_real_isolator_hides_peer_memory_tokens_and_host_proc_while_cli_socket_works(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "allowed.txt").write_text("current workspace")
    service = tmp_path / "service"
    service.mkdir()
    private = service / "memory"
    private.mkdir()
    (private / "alice.md").write_text("private Alice memory")
    (service / "config.toml").write_text('token = "service-secret"')
    (service / "state.sqlite3").write_text("private task/history metadata")
    other_home = service / "contexts" / "other" / ".codex"
    other_home.mkdir(parents=True)
    (other_home / "history.jsonl").write_text("another user's history")
    (workspace / "escape").symlink_to(private / "alice.md")
    home = service / "contexts" / "current"
    controls = tmp_path / "controls"
    controls.mkdir()
    control = controls / "current.json"
    control.write_text(json.dumps({"token": "current-task-token", "socket": str(controls / "socket")}))
    other_control = controls / "other.json"
    other_control.write_text("other-task-token")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(controls / "socket"))
    listener.listen()
    listener.settimeout(20)
    received = []

    def respond():
        try:
            connection, _ = listener.accept()
            with connection:
                received.append(json.loads(connection.recv(4096)))
                connection.sendall(b'{"authorized":true}\n')
        except (TimeoutError, OSError):
            return

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    forbidden = [
        str(private / "alice.md"),
        str(service / "config.toml"),
        str(service / "state.sqlite3"),
        str(other_home / "history.jsonl"),
        str(other_control),
        str(workspace / "escape"),
        f"/proc/{os.getpid()}/root{private / 'alice.md'}",
        f"/proc/1/root{private / 'alice.md'}",
        f"/proc/self/root{private / 'alice.md'}",
    ]
    script = f"""
import json, os, pathlib, socket
forbidden = {forbidden!r}
assert all(not pathlib.Path(path).exists() for path in forbidden)
assert 'NYANPASU_TOKEN' not in os.environ
assert pathlib.Path('allowed.txt').read_text() == 'current workspace'
control = json.loads(pathlib.Path({str(control)!r}).read_text())
client = socket.socket(socket.AF_UNIX)
client.connect(control['socket'])
client.sendall(json.dumps({{'token': control['token']}}).encode())
response = json.loads(client.recv(4096))
pathlib.Path(os.environ['HOME'], 'owned.txt').write_text('current context')
print(json.dumps({{'response': response, 'visible_controls': sorted(p.name for p in pathlib.Path({str(controls)!r}).iterdir())}}))
"""
    try:
        observed = _run(
            ExecutionIsolation(home, control, controls / "socket"),
            workspace,
            script,
            env={"PATH": os.defpath, "NYANPASU_TOKEN": "service-secret"},
        )
        assert observed == {"response": {"authorized": True}, "visible_controls": ["current.json", "socket"]}
        assert received == [{"token": "current-task-token"}]
        assert (home / "owned.txt").read_text() == "current context"
        assert (private / "alice.md").read_text() == "private Alice memory"
    finally:
        listener.close()
        thread.join(timeout=1)


def test_real_explicit_tool_grant_is_readonly(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = tmp_path / "operator-tool.py"
    tool.write_text("operator supplied tool")
    script = f"""
import json, pathlib
path = pathlib.Path({str(tool)!r})
assert path.read_text() == 'operator supplied tool'
try:
    path.write_text('overwritten')
except OSError:
    print(json.dumps({{'readonly': True}}))
else:
    raise AssertionError('readonly grant was writable')
"""
    assert _run(ExecutionIsolation(tmp_path / "home", readonly_paths=(tool,)), workspace, script) == {"readonly": True}
    assert tool.read_text() == "operator supplied tool"


def test_native_executables_boot_inside_real_isolation(tmp_path):
    if shutil.which("codex") is None or shutil.which("claude") is None:
        pytest.skip("native CLIs are not installed in this test environment")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = """
import json, subprocess
versions = {}
for command in ['codex', 'claude']:
    result = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    versions[command] = result.stdout.strip()
print(json.dumps(versions))
"""
    observed = _run(ExecutionIsolation(tmp_path / "home"), workspace, script, env={"PATH": os.environ["PATH"]})
    assert "codex-cli" in observed["codex"]
    assert "Claude Code" in observed["claude"]
