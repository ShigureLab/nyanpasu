from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import psutil
import pytest

from nyanpasu.resources import ProcessResources


@pytest.fixture
def process_tree(monkeypatch):
    class Process:
        def __init__(self, pid, *, cpu=0, rss=100, created=1, children=()):
            self.pid = pid
            self.cpu = cpu
            self.rss = rss
            self.created = created
            self.descendants = list(children)
            self.error = None

        def oneshot(self):
            return nullcontext()

        def create_time(self):
            if self.error:
                raise self.error
            return self.created

        def cpu_times(self):
            return SimpleNamespace(user=self.cpu, system=0)

        def memory_info(self):
            return SimpleNamespace(rss=self.rss)

        def children(self, *, recursive):
            assert recursive
            return self.descendants

    child = Process(2, cpu=10, rss=200)
    first = Process(1, cpu=5, children=[child])
    second = Process(3, cpu=2, rss=300)
    processes = {process.pid: process for process in [first, child, second]}

    def lookup(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]

    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr("nyanpasu.resources.psutil.Process", lookup)
    monkeypatch.setattr("nyanpasu.resources.time.monotonic", lambda: clock.now)
    return processes, clock


def test_cpu_deltas_and_rss_include_children_and_all_instances_without_double_counting(process_tree):
    processes, clock = process_tree
    sampler = ProcessResources()
    roots = {"codex": [1, 3, 1], "claude": [], "nested": [1, 2]}
    first = sampler.sample(roots)
    assert first["codex"]["cpu_percent"] is None
    assert first["codex"]["rss_bytes"] == 600
    assert first["codex"]["process_count"] == 3
    assert [row["pid"] for row in first["codex"]["instances"]] == [1, 3]
    assert first["nested"]["rss_bytes"] == 300
    assert first["claude"] == {"cpu_percent": 0, "rss_bytes": 0, "process_count": 0, "instances": []}

    clock.now += 2
    processes[1].cpu += 0.5
    processes[2].cpu += 2
    processes[3].cpu += 1
    second = sampler.sample(roots)
    assert second["codex"]["cpu_percent"] == 175  # 100% is one core, not the whole host.
    assert second["codex"]["instances"][0]["cpu_percent"] == 125
    assert second["codex"]["instances"][1]["cpu_percent"] == 50


def test_exiting_processes_and_reused_pids_do_not_keep_stale_measurements(process_tree):
    processes, clock = process_tree
    sampler = ProcessResources()
    sampler.sample({"codex": [1, 3]})
    clock.now += 1
    processes[2].error = psutil.NoSuchProcess(2)  # Exits between discovery and reading.
    del processes[3]
    usage = sampler.sample({"codex": [1, 3]})["codex"]
    assert usage["process_count"] == 1
    assert usage["rss_bytes"] == 100
    assert usage["cpu_percent"] == 0

    clock.now += 1
    processes[1].created += 1
    processes[1].cpu = 100
    assert sampler.sample({"codex": [1]})["codex"]["cpu_percent"] is None
    sampler.sample({"codex": []})
    clock.now += 1
    assert sampler.sample({"codex": [1]})["codex"]["cpu_percent"] is None


def test_unreadable_processes_are_unavailable_instead_of_reporting_zero(process_tree):
    processes, _ = process_tree
    processes[2].error = psutil.AccessDenied(2)
    usage = ProcessResources().sample({"codex": [1]})["codex"]
    assert usage["rss_bytes"] is None
    assert usage["cpu_percent"] is None
    assert usage["process_count"] == 2
