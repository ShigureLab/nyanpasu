from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from courier import export
from receiver import Receiver


class ExportTests(unittest.TestCase):
    def test_export_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("red\ngreen\nblue\n", encoding="utf-8")
            receiver = Receiver(root / "receiver.sqlite3")
            export(source, receiver)
            export(source, receiver)
            self.assertEqual(receiver.records(str(source.resolve())), ["red", "green", "blue"])


if __name__ == "__main__":
    unittest.main()
