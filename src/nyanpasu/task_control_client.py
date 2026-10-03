from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any


def command(control: Path) -> list[str]:
    return [str(Path(sys.executable).resolve()), "-I", "-S", str(Path(__file__).resolve()), str(control)]


def call_control(control: Path, request: dict[str, Any]) -> dict[str, Any]:
    capability = json.loads(control.read_text())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(capability["socket"])
        client.sendall(json.dumps({**request, "token": capability["token"]}).encode() + b"\n")
        with client.makefile("rb") as reader:
            response = json.loads(reader.readline())
    if not response["ok"]:
        raise ValueError(response["error"])
    return response["result"]


def main() -> None:
    try:
        raw = sys.stdin.read() if sys.argv[2] == "-" else Path(sys.argv[2]).read_text()
        print(json.dumps(call_control(Path(sys.argv[1]), json.loads(raw)), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
