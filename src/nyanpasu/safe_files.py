from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def open_regular_file(root: Path, relative_path: str | Path) -> Iterator[BinaryIO]:
    """Read an agent-owned regular file without following any symlinks.

    The service owns the stable root and its ancestors. Everything underneath
    may change concurrently: walking directory descriptors keeps each lookup
    anchored to the directory actually opened, even if its name is replaced.
    Callers must inspect size and read using this stream, never reopen its path.
    """
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("file path must be relative and stay within its root")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        # NONBLOCK prevents a malicious FIFO from blocking before fstat rejects
        # it. It has no effect on the regular files this reader permits.
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("file must be a regular file")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream
