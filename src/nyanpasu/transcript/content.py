from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

PREVIEW_BYTES = 4096
CHUNK_BYTES = 64 * 1024
SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b|(?i:Bearer\s+)[A-Za-z0-9._~+/-]{12,}"
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def encode(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode(value: str) -> Any:
    if len(value) > 4096:
        raise ValueError("Invalid content reference or cursor")
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid content reference or cursor") from exc


def content_ref(session_id: str, entry_id: str, block_id: str, text: str) -> str:
    return encode([session_id, entry_id, block_id, fingerprint(text)])


def content_page(text: str, ref: str, offset: int, limit: int, *, tail: bool = False) -> dict[str, Any]:
    data = text.encode("utf-8")
    if tail:
        offset = max(0, len(data) - limit)
        while offset < len(data) and data[offset] & 0xC0 == 0x80:
            offset += 1
    if offset < 0 or offset > len(data) or (offset < len(data) and data[offset] & 0xC0 == 0x80):
        raise ValueError("Offset must be a UTF-8 byte boundary within this content")
    value = data[offset : offset + limit].decode("utf-8", errors="ignore")
    end = offset + len(value.encode("utf-8"))
    return {
        "content_ref": ref,
        "text": value,
        "offset": offset,
        "next_offset": end if end < len(data) else None,
        "recorded_bytes": len(data),
    }
