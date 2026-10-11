from __future__ import annotations

from typing import Any

from nyanpasu.redaction import redact
from nyanpasu.transcript.content import content
from nyanpasu.transcript.history import HistoryItem
from nyanpasu.transcript.models import EntryUpdate


def instruction_item(
    identity: str,
    text: str,
    *,
    title: str,
    raw: dict[str, Any],
    recorded_at: str | None = None,
    origin: str = "native",
) -> HistoryItem:
    safe = redact(raw)
    safe_text = redact(text)
    return HistoryItem(
        id=identity,
        presentation=EntryUpdate(
            kind="instructions", title=title, state="recorded", blocks=[content("instructions", "text", safe_text)]
        ),
        raw=safe,
        redacted=safe != raw or safe_text != text,
        origin=origin,
        recorded_at=recorded_at,
    )
