from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nyanpasu import task_control_client

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_SYSTEM_DIRECTORIES = (Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64"))
_SYSTEM_CONFIGURATION = (
    Path("/etc/ld.so.cache"),
    Path("/etc/alternatives"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/passwd"),
    Path("/etc/group"),
    Path("/etc/hosts"),
    Path("/etc/resolv.conf"),
    Path("/etc/ssl/certs"),
    Path("/etc/ssl/openssl.cnf"),
    Path("/etc/pki/tls/certs"),
    Path("/etc/localtime"),
    Path("/etc/timezone"),
)
_BROAD_PATHS = frozenset(
    Path(path) for path in ("/", "/home", "/root", "/data", "/tmp", "/run", "/proc", "/sys", "/dev")
)


def _absolute(path: Path) -> Path:
    # Collapse '..' while preserving executable/virtualenv symlink paths.
    return Path(os.path.abspath(Path(path).expanduser()))  # noqa: PTH100


def _control_runtime_paths() -> tuple[Path, ...]:
    # Use the base interpreter and its stdlib, never the service's virtualenv.
    paths = {Path(value).resolve() for value in (sys.executable, sys.base_prefix, sys.base_exec_prefix)}
    return tuple(
        path
        for path in sorted(paths)
        if not any(path.is_relative_to(system.resolve()) for system in _SYSTEM_DIRECTORIES)
        and not any(path != parent and path.is_relative_to(parent) for parent in paths)
    )


def _native_home(home: Path, driver: str, directory: Path | None = None) -> Path:
    if driver not in {"codex", "claude-code"}:
        raise ValueError(f"unsupported isolation driver: {driver}")
    directory = directory if directory is not None else Path(".codex" if driver == "codex" else ".claude")
    if directory.is_absolute() or not directory.parts or ".." in directory.parts:
        raise ValueError("native_directory must be a relative directory within the isolated home")
    return home / directory


def _seed_directory(home: Path, relative: Path) -> Path:
    directory = home
    for component in relative.parts:
        directory /= component
        if directory.is_symlink():
            raise ValueError("seeded directories must not be symlinks")
        directory.mkdir(exist_ok=True, mode=0o700)
    return directory


def _seed_file(source: Path, target: Path) -> None:
    if target.is_symlink():
        raise ValueError("seeded configuration must not be a symlink")
    if target.exists():
        if not target.is_file():
            raise ValueError("seeded configuration must be a regular file")
        return
    # Replace from a private temporary file rather than opening a backend-
    # writable destination: a symlink must never redirect a service write.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".seed-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(source.read_bytes())
            stream.flush()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def seed_home(
    home: Path,
    driver: str,
    source_env: Mapping[str, str],
    *,
    native_directory: Path | None = None,
    template: Path | None = None,
) -> Path:
    """Seed authentication/configuration, never personal memories or histories.

    Existing per-context files are retained, including refreshed credentials.
    Call this while the context has no running backend. Operator changes to the
    template require an explicit reseed/new context, not an implicit home copy.
    """
    home = _absolute(home)
    if home.is_symlink():
        raise ValueError("isolated home must not be a symlink")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    native = _native_home(home, driver, native_directory)
    if template is not None:
        # This is an explicit administrator-authored minimal HOME tree, not a
        # copy of the operator's native history or automatically discovered home.
        template = _absolute(template)
        if template.is_symlink() or not template.is_dir():
            raise ValueError("home template must be a directory without symlinks")
        entries = sorted(template.rglob("*"))
        for source in entries:
            if source.is_symlink() or not (source.is_file() or source.is_dir()):
                raise ValueError("home template must contain only regular files and directories, without symlinks")
        for source in entries:
            relative = source.relative_to(template)
            if source.is_dir():
                _seed_directory(home, relative)
            else:
                parent = _seed_directory(home, relative.parent)
                _seed_file(source, parent / source.name)
        _seed_directory(home, native.relative_to(home))
        return native
    _seed_directory(home, native.relative_to(home))
    source_home = Path(source_env.get("HOME", str(Path.home())))
    if driver == "codex":
        source = Path(source_env.get("CODEX_HOME", str(source_home / ".codex")))
        names = ("config.toml", "auth.json")
    else:
        source = Path(source_env.get("CLAUDE_CONFIG_DIR", str(source_home / ".claude")))
        names = ("settings.json", ".credentials.json")
    for name in names:
        target = native / name
        if (source / name).is_file():
            _seed_file(source / name, target)
    return native


@dataclass(frozen=True)
class ExecutionIsolation:
    """The entire per-turn filesystem capability granted by the service.

    Run every model process with this boundary, including history helper servers.
    An unisolated sibling with the same host UID would defeat file isolation.
    readonly_paths are operator grants for executable code and tools, not model
    input. They must not contain service state, another context, or user homes.

    Networking remains available. The service must require authentication and
    keep its administrative token out of backend credentials/configuration.
    """

    home: Path
    control_file: Path | None = None
    control_socket: Path | None = None
    readonly_paths: tuple[Path, ...] = ()
    bwrap: str = "/usr/bin/bwrap"

    def wrap(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
        if not argv:
            raise ValueError("an isolated process requires a command")
        if (self.control_file is None) != (self.control_socket is None):
            raise ValueError("control_file and control_socket must be supplied together")
        if shutil.which(self.bwrap) is None:
            raise RuntimeError("bubblewrap is required for isolated execution")
        home = _absolute(self.home)
        cwd = _absolute(cwd)
        if home in _BROAD_PATHS or cwd in _BROAD_PATHS or home.is_relative_to(cwd):
            raise ValueError("isolated home and workspace must be separate, specific directories")
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not cwd.is_dir():
            raise ValueError("isolated workspace does not exist")
        controls = ()
        if self.control_file is not None and self.control_socket is not None:
            control_file, control_socket = _absolute(self.control_file), _absolute(self.control_socket)
            if not control_file.is_file() or not stat.S_ISSOCK(control_socket.stat().st_mode):
                raise ValueError("isolated task control requires a file and a Unix socket")
            controls = (control_file, control_socket)
        original_home = _absolute(Path(env.get("HOME", str(Path.home()))))
        native_homes = {
            _absolute(Path(env.get("CODEX_HOME", str(original_home / ".codex")))),
            _absolute(Path(env.get("CLAUDE_CONFIG_DIR", str(original_home / ".claude")))),
        }
        extra_paths = []
        for path in (*self.readonly_paths, *(_control_runtime_paths() if controls else ())):
            path = _absolute(path)
            resolved = path.resolve()
            if resolved in {item.resolve() for item in _BROAD_PATHS | native_homes | {original_home}}:
                raise ValueError("readonly_paths cannot expose a whole host home, native home, or system state tree")
            if any(protected.resolve().is_relative_to(resolved) for protected in (home, cwd, *controls)):
                raise ValueError(
                    "readonly_paths cannot expose a parent of the current home, workspace, or task control"
                )
            if not path.exists():
                raise ValueError(f"readonly path does not exist: {path}")
            extra_paths.append(path)
        command = [
            self.bwrap,
            "--unshare-all",
            "--share-net",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
        ]
        for path in _SYSTEM_DIRECTORIES:
            if path.is_symlink():
                command.extend(("--symlink", str(path.readlink()), str(path)))
            elif path.exists():
                command.extend(("--ro-bind", str(path), str(path)))
        for path in _SYSTEM_CONFIGURATION:
            if path.exists():
                command.extend(("--ro-bind", str(path), str(path)))
        command.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/run",
                "--bind",
                str(home),
                str(home),
                "--bind",
                str(cwd),
                str(cwd),
            )
        )
        for path in (*extra_paths, *controls):
            command.extend(("--ro-bind", str(path), str(path)))
        if controls:
            command.extend(
                ("--ro-bind", str(Path(task_control_client.__file__).resolve()), str(task_control_client.ISOLATED_PATH))
            )
        command.extend(("--chdir", str(cwd), "--", *argv))
        # process_env already selects explicit backend credentials. Only the
        # service's own configuration namespace must never cross this boundary.
        isolated_env = {
            key: value
            for key, value in env.items()
            if key not in {"NYANPASU_HOME", "NYANPASU_TOKEN"} and not key.startswith("NYANPASU__")
        }
        for key, default in (("CODEX_HOME", ".codex"), ("CLAUDE_CONFIG_DIR", ".claude")):
            requested = Path(env.get(key, str(home / default)))
            isolated_env[key] = str(
                requested
                if requested.is_absolute() and requested.resolve().is_relative_to(home.resolve())
                else home / default
            )
        isolated_env.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_CACHE_HOME=str(home / ".cache"),
            XDG_DATA_HOME=str(home / ".local" / "share"),
            XDG_STATE_HOME=str(home / ".local" / "state"),
            XDG_RUNTIME_DIR="/tmp/runtime",
            TMPDIR="/tmp",
        )
        return command, isolated_env
