from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from scripts.reviewer_eval_isolate import isolated_command


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("unshare"), reason="Linux mount namespaces required")
def test_same_absolute_temp_paths_are_private_to_each_home_and_its_children():
    cache = Path(__file__).resolve().parents[1] / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    marker = f"reviewer-isolation-{uuid.uuid4().hex}"
    host_paths = [Path(prefix) / marker for prefix in ("/tmp", "/var/tmp")]
    code = """
import os, subprocess, sys
from pathlib import Path
marker, tag = sys.argv[1:]
for prefix in ['/tmp', '/var/tmp']:
    Path(prefix, marker).write_text(tag)
print('ready', flush=True)
sys.stdin.read(1)
for prefix in ['/tmp', '/var/tmp']:
    assert Path(prefix, marker).read_text() == tag
subprocess.run([sys.executable, '-c',
    'import sys; from pathlib import Path; assert Path("/tmp",sys.argv[1]).read_text()==sys.argv[2]',
    marker, tag], check=True)
assert os.environ['TMPDIR'] == '/tmp'
"""
    processes = []
    try:
        for path in host_paths:
            path.write_text("host")
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            homes = [Path(directory) / arm for arm in ("A", "B")]
            for tag, home in zip(("A", "B"), homes, strict=True):
                processes.append(
                    subprocess.Popen(
                        isolated_command(home, [sys.executable, "-c", code, marker, tag]),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            for process in processes:
                assert process.stdout is not None
                assert process.stdout.readline().strip() == "ready"
            for process in processes:
                output, error = process.communicate("x", timeout=20)
                assert process.returncode == 0, (output, error)
            for tag, home in zip(("A", "B"), homes, strict=True):
                for directory_name in ("tmp", "var-tmp"):
                    assert (home / "temporary" / directory_name / marker).read_text() == tag
            namespaces = [(home / "temporary/mount-namespace").read_text().strip() for home in homes]
            assert len(set(namespaces)) == 2
            assert str(Path("/proc/self/ns/mnt").readlink()) not in namespaces
            assert all(path.read_text() == "host" for path in host_paths)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
        for path in host_paths:
            path.unlink(missing_ok=True)


def test_home_cannot_be_hidden_by_its_private_mount():
    with pytest.raises(ValueError, match="outside"):
        isolated_command(Path("/tmp/evaluation"), ["true"])
