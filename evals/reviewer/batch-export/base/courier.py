from __future__ import annotations

import argparse
from pathlib import Path

from receiver import Receiver


def export(source: Path, receiver: Receiver) -> None:
    records = source.read_text(encoding="utf-8").splitlines()
    for offset in range(0, len(records), 2):
        receiver.deliver(str(source.resolve()), offset, records[offset : offset + 2])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("receiver", type=Path)
    args = parser.parse_args()
    export(args.source, Receiver(args.receiver))
