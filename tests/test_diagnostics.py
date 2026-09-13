from __future__ import annotations

from datetime import datetime

from nyanpasu.diagnostics import diagnostic


def test_native_log_timestamp_severity_and_message_are_readable():
    entry = diagnostic("\x1b[2m2026-09-14T00:00:01.123Z\x1b[0m WARN codex_core::network: retrying 请求\n")
    assert entry == {
        "timestamp": "2026-09-14T00:00:01.123Z",
        "level": "warn",
        "target": "codex_core::network",
        "message": "retrying 请求",
    }


def test_plain_stderr_is_preserved_without_inventing_a_severity():
    entry = diagnostic("connection closed\n")
    assert entry["message"] == "connection closed"
    assert entry["level"] == "stderr" and entry["target"] is None
    assert datetime.fromisoformat(entry["timestamp"])
