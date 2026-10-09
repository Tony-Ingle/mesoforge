"""Shared process lock preventing heavy Guidance and Forecast work from overlapping."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mesoforge.application.worker_status import status_directory

_ROOT = Path(__file__).resolve().parents[3]


def worker_lock_root(runtime_root: Path) -> Path:
    """Optionally share one host-volume lock across independently selected roots."""
    configured = os.environ.get("MESOFORGE_WORKER_LOCK_ROOT")
    if configured is None:
        return runtime_root
    path = Path(configured)
    resolved = path.resolve()
    if (
        not path.is_absolute()
        or resolved.is_relative_to(_ROOT)
        or resolved == Path(resolved.anchor)
    ):
        raise ValueError("Worker lock root must be an absolute directory outside the checkout")
    return resolved


@contextmanager
def single_writer(root: Path) -> Iterator[bool]:
    """Keep the existing lock path so older Guidance workers participate too."""
    directory = status_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "guidance-worker.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
