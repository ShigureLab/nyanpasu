from __future__ import annotations

from typing import Any

from nyanpasu.redaction import redact

TITLE_LENGTH = 140


def task_execution(task: dict[str, Any]) -> dict[str, Any]:
    """Display the admitted target, preserving unknown targets in historical tasks."""
    return {"kind": task.get("kind", "default"), "execution": task.get("execution")}


def task_title(task: dict[str, Any]) -> str:
    metadata = task.get("metadata", {})
    request = metadata.get("request")
    if isinstance(request, dict):
        for value in (request.get("title"), request.get("task")):
            if isinstance(value, str) and value.strip():
                return _first_line(value)
    pull_request = metadata.get("pull_request")
    if isinstance(pull_request, dict):
        repo = pull_request.get("repo") or "GitHub PR"
        number = pull_request.get("number")
        suffix = [metadata.get("github_event"), metadata.get("review_mode")]
        parts = [str(repo), f"#{number}" if number else ""]
        parts.extend(value for value in suffix if isinstance(value, str) and value)
        return _first_line(" ".join(parts))
    prompt = task.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return _first_line(prompt)
    return _first_line(str(task.get("task_id") or "Task"))


def _first_line(value: str) -> str:
    line = redact(" ".join(value.strip().splitlines()[0].split()))
    ellipsis = "..."
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - len(ellipsis)] + ellipsis
