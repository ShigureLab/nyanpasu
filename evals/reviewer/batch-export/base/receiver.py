from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class Receiver:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries "
                "(source TEXT, offset INTEGER, payload TEXT, PRIMARY KEY (source, offset))"
            )

    def deliver(self, source: str, offset: int, records: list[str]) -> None:
        payload = json.dumps(records)
        with sqlite3.connect(self.path) as db:
            existing = db.execute(
                "SELECT payload FROM deliveries WHERE source = ? AND offset = ?", (source, offset)
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ValueError("request reused with different records")
                return
            db.execute("INSERT INTO deliveries VALUES (?, ?, ?)", (source, offset, payload))

    def records(self, source: str) -> list[str]:
        with sqlite3.connect(self.path) as db:
            batches = db.execute("SELECT payload FROM deliveries WHERE source = ? ORDER BY offset", (source,))
            return [record for (payload,) in batches for record in json.loads(payload)]
