from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _absolute(path: Path) -> Path:
    # Preserve symlinks so seeding can reject them before writing configuration.
    return Path(os.path.abspath(Path(path).expanduser()))  # noqa: PTH100


def _native_home(home: Path, driver: str, directory: Path | None = None) -> Path:
    if driver not in {"codex", "claude-code"}:
        raise ValueError(f"unsupported native home driver: {driver}")
    directory = directory if directory is not None else Path(".codex" if driver == "codex" else ".claude")
    if directory.is_absolute() or not directory.parts or ".." in directory.parts:
        raise ValueError("native_directory must be a relative directory within the context home")
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
        raise ValueError("context home must not be a symlink")
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


def native_env(env: Mapping[str, str], *, home: Path | None = None) -> dict[str, str]:
    """Keep service settings out of CLIs and select their per-context state paths."""
    result = {
        key: value
        for key, value in env.items()
        if key not in {"NYANPASU_HOME", "NYANPASU_TOKEN"} and not key.startswith("NYANPASU__")
    }
    if home is None:
        return result
    home = _absolute(home)
    for key, default in (("CODEX_HOME", ".codex"), ("CLAUDE_CONFIG_DIR", ".claude")):
        requested = Path(result.get(key, str(home / default)))
        result[key] = str(
            requested
            if requested.is_absolute() and requested.resolve().is_relative_to(home.resolve())
            else home / default
        )
    result.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
    )
    return result
