"""Run one evaluation home and all descendants with persistent private temp mounts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def isolated_command(home: Path, command: list[str]) -> list[str]:
    home = home.resolve()
    if any(home.is_relative_to(path) for path in (Path("/tmp"), Path("/var/tmp"))):
        raise ValueError("evaluation home must be outside the temporary paths it mounts over")
    for name in ("tmp", "var-tmp"):
        (home / "temporary" / name).mkdir(parents=True, exist_ok=True)
    return [
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--propagation",
        "private",
        "--fork",
        "--kill-child",
        "--",
        "sh",
        "-ec",
        'mount --bind "$1/tmp" /tmp\n'
        'mount --bind "$1/var-tmp" /var/tmp\n'
        'readlink /proc/self/ns/mnt > "$1/mount-namespace"\n'
        "export TMPDIR=/tmp TMP=/tmp TEMP=/tmp\n"
        "shift\n"
        'exec "$@"',
        "reviewer-eval-isolate",
        str(home / "temporary"),
        *command,
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("home", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("supply a command after --")
    argv = isolated_command(args.home, command)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
