from __future__ import annotations

import time
from threading import Lock
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


def _totals(processes: Iterable[dict]) -> dict:
    entries = list(processes)
    cpu = [entry["cpu_percent"] for entry in entries if entry["cpu_percent"] is not None]
    readable = all(entry["rss_bytes"] is not None for entry in entries)
    return {
        "cpu_percent": (sum(cpu) if cpu or not entries else None) if readable else None,
        "rss_bytes": sum(entry["rss_bytes"] for entry in entries) if readable else None,
        "process_count": len(entries),
    }


class ProcessResources:
    """Sample owned process trees without blocking for a CPU measurement interval."""

    def __init__(self) -> None:
        self._previous: dict[tuple[int, float], tuple[float, float]] = {}
        self._lock = Lock()

    def _read(self, process: psutil.Process, now: float, current: dict) -> dict | None:
        try:
            with process.oneshot():
                identity = (process.pid, process.create_time())
                cpu = process.cpu_times()
                seconds = cpu.user + cpu.system
                rss = process.memory_info().rss
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied:
            return {"cpu_percent": None, "rss_bytes": None}
        previous = self._previous.get(identity)
        current[identity] = (now, seconds)
        percent = None
        if previous is not None and now > previous[0]:
            percent = max(0.0, seconds - previous[1]) / (now - previous[0]) * 100
        return {"cpu_percent": percent, "rss_bytes": rss}

    def sample(self, backends: Mapping[str, Sequence[int]]) -> dict[str, dict]:
        # Runtime HTTP handlers run in worker threads and share these baselines.
        with self._lock:
            now = time.monotonic()
            current: dict[tuple[int, float], tuple[float, float]] = {}
            measured: dict[int, dict | None] = {}
            result = {}
            for name, roots in backends.items():
                owned = {}
                instances = []
                for pid in sorted(set(roots)):
                    try:
                        root = psutil.Process(pid)
                        tree = [root, *root.children(recursive=True)]
                    except psutil.NoSuchProcess:
                        continue
                    except psutil.AccessDenied:
                        owned[pid] = {"cpu_percent": None, "rss_bytes": None}
                        instances.append({"pid": pid, **_totals([owned[pid]])})
                        continue
                    entries = {}
                    for process in tree:
                        if process.pid not in measured:
                            measured[process.pid] = self._read(process, now, current)
                        if (entry := measured[process.pid]) is not None:
                            entries[process.pid] = entry
                    if entries:
                        owned.update(entries)
                        instances.append({"pid": pid, **_totals(entries.values())})
                result[name] = {**_totals(owned.values()), "instances": instances}
            # New processes warm up on their first observation; departed ones are forgotten.
            self._previous = current
            return result
