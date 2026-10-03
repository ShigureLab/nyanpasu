#!/usr/bin/env python3
"""One-time migration of legacy config.toml; runtime loading has no compatibility path."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from nyanpasu.config import NyanpasuConfig


def migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    data = copy.deepcopy(raw)
    if "backends" in data or "tasks" in data or "memory" in data:
        raise ValueError("input already contains new configuration sections; migrate only a legacy file")
    selected = data.get("runtime", {}).pop("backend", "codex")
    if selected not in {"codex", "claude"}:
        raise ValueError(f"unknown legacy backend: {selected}")
    backends = {}
    timeouts = {}
    for name, driver in (("codex", "codex"), ("claude", "claude-code")):
        old = data.pop(name, {})
        default_args = [] if name == "codex" else ["--permission-prompts", "none", "--system-prompt-snapshot", "off"]
        process = {"command": [old.pop("bin", name), *old.pop("args", default_args)]}
        for field in ("pass_env", "env"):
            if field in old:
                process[field] = old.pop(field)
        if isinstance(process.get("pass_env"), str):
            process["pass_env"] = [name.strip() for name in process["pass_env"].split(",") if name.strip()]
        defaults = {}
        for before, after in (("model", "model"), ("reasoning_effort", "reasoning")):
            if before in old:
                defaults[after] = old.pop(before)
        timeouts[name] = old.pop("command_timeout_seconds", 3600)
        if "fallback_models" in old:
            for item in old["fallback_models"]:
                if isinstance(item, dict) and "reasoning_effort" in item:
                    item["reasoning"] = item.pop("reasoning_effort")
        backends[name] = {"driver": driver, "process": process, "defaults": defaults, "options": old}
    data["backends"] = backends
    data["tasks"] = {
        "defaults": {
            "execution": {"backend": selected},
            "limits": {"turn_timeout_seconds": timeouts[selected]},
        }
    }
    settings = data.pop("plugins", {})
    enabled = data.pop("enabled_plugins", ())
    if isinstance(enabled, str):
        enabled = [name.strip() for name in enabled.split(",") if name.strip()]
    enabled = enabled or tuple(settings)
    data["plugins"] = {"enabled": list(enabled), "settings": settings}
    notes = []
    inactive = "claude" if selected == "codex" else "codex"
    if timeouts[inactive] != timeouts[selected]:
        notes.append(
            f"Retained active {selected} timeout {timeouts[selected]} seconds. "
            f"Set tasks.kinds.<kind>.limits explicitly to retain inactive {inactive} timeout "
            f"{timeouts[inactive]} seconds when routing tasks there."
        )
    NyanpasuConfig.model_validate(data)
    return data, notes


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_value(key)} = {toml_value(item)}" for key, item in value.items()) + "}"
    raise ValueError(f"unsupported TOML configuration value: {type(value).__name__}")


def render_toml(data: dict[str, Any], path: tuple[str, ...] = ()) -> str:
    lines = ["[" + ".".join(toml_value(part) for part in path) + "]"] if path else []
    lines.extend(
        f"{toml_value(key)} = {toml_value(value)}" for key, value in data.items() if not isinstance(value, dict)
    )
    for key, value in data.items():
        if isinstance(value, dict):
            lines.extend(["", render_toml(value, (*path, key))])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, help="new file; existing files are never overwritten")
    args = parser.parse_args()
    data, notes = migrate(tomllib.loads(args.source.read_text(encoding="utf-8")))
    rendered = render_toml(data)
    # Verify serialization before touching the destination, which may contain credentials.
    NyanpasuConfig.model_validate(tomllib.loads(rendered))
    descriptor = os.open(args.destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(rendered)
    print(f"Wrote {args.destination}; inspect it before replacing the active configuration.")
    for note in notes:
        print(note, file=sys.stderr)
    print(
        "Environment overrides now use NYANPASU__<SECTION>__<FIELD>; migrate service environment "
        "separately (for example NYANPASU_TOKEN -> NYANPASU__SERVER__TOKEN).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
