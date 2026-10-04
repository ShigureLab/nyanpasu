"""Evaluator-only behavioral witnesses; never include these in reviewer input."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv.pop(1)).resolve()))

from checkpoint import CheckpointStore
from courier import export
from receiver import Receiver


class RejectBeforeCommit(Receiver):
    def deliver(self, source: str, offset: int, records: list[str]) -> None:
        raise ConnectionError("connection failed before acceptance")


class LoseAcknowledgement(Receiver):
    def deliver(self, source: str, offset: int, records: list[str]) -> None:
        super().deliver(source, offset, records)
        raise ConnectionError("connection failed after acceptance")


class ExportEvidence(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.txt"
        self.receiver_path = self.root / "receiver.sqlite3"
        self.checkpoint_path = self.root / "checkpoint.json"

    def write_source(self, records: list[str]) -> None:
        self.source.write_text("\n".join(records) + "\n", encoding="utf-8")

    def run_export(self, receiver_type=Receiver) -> None:
        export(self.source, receiver_type(self.receiver_path), CheckpointStore(self.checkpoint_path))

    def received(self) -> list[str]:
        return Receiver(self.receiver_path).records(str(self.source.resolve()))

    def test_each_input_occurrence_is_delivered(self):
        self.write_source(["red", "green", "red", "blue"])
        self.run_export()
        self.assertEqual(self.received(), ["red", "green", "red", "blue"])

    def test_retry_after_rejection_preserves_all_records(self):
        self.write_source(["red", "green", "blue"])
        with self.assertRaises(ConnectionError):
            self.run_export(RejectBeforeCommit)
        self.assertEqual(self.received(), [])
        self.run_export()
        self.assertEqual(self.received(), ["red", "green", "blue"])

    def test_retry_after_lost_acknowledgement_delivers_once(self):
        self.write_source(["red", "green", "blue"])
        with self.assertRaises(ConnectionError):
            self.run_export(LoseAcknowledgement)
        self.assertEqual(self.received(), ["red", "green"])
        self.run_export()
        self.assertEqual(self.received(), ["red", "green", "blue"])


if __name__ == "__main__":
    unittest.main()
