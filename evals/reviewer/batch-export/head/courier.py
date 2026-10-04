from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from checkpoint import CheckpointStore
from receiver import Receiver


def export(source: Path, receiver: Receiver, checkpoints: CheckpointStore) -> None:
    records = source.read_text(encoding="utf-8").splitlines()
    state = checkpoints.load()
    seen = set(state["seen"])
    for offset in range(state["offset"], len(records), 2):
        batch = records[offset : offset + 2]
        identifiers = {hashlib.sha256(record.encode("utf-8")).hexdigest() for record in batch}
        if seen & identifiers:
            raise ValueError("record already exported")
        seen.update(identifiers)
        checkpoints.save({"offset": offset + len(batch), "seen": sorted(seen)})
        receiver.deliver(str(source.resolve()), offset, batch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("receiver", type=Path)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()
    export(args.source, Receiver(args.receiver), CheckpointStore(args.checkpoint))
