from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TypedDict

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s+(?:([\w:.-]+):\s+)?(.*)$")


class Diagnostic(TypedDict):
    timestamp: str
    level: str
    target: str | None
    message: str


def diagnostic(line: str) -> Diagnostic:
    text = ANSI.sub("", line).rstrip()
    match = LOG_LINE.match(text)
    if match:
        timestamp, level, target, message = match.groups()
        return {"timestamp": timestamp, "level": level.lower(), "target": target, "message": message}
    return {"timestamp": datetime.now(UTC).isoformat(), "level": "stderr", "target": None, "message": text}
