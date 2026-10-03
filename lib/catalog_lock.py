"""Cross-process lock for the master catalog files.

Three writers touch ``output/5-catalog/catalog.csv`` and ``assignments.csv``:
the catalog pipeline step, the dashboard (reviewer saves), and maintenance
scripts (dedup, add_candidates). Each does a read-modify-write of the whole
file. Without a lock shared *across processes*, a reviewer's save that lands
between the pipeline's read and its write is silently overwritten.

The lock is an ``fcntl.flock`` on ``output/5-catalog/.catalog.lock``. flock
is released automatically when the holder's file descriptor closes, so a
crashed holder never leaves a stale lock. The lock file itself is never
written to and is safe to delete when nothing is running.

Usage::

    with catalog_lock(catalog_dir, timeout=60):
        rows = read(...)
        ...
        atomic_write(...)

Acquisition polls ``LOCK_NB`` so a wait can time out instead of hanging a
stage forever; ``CatalogLockTimeout`` is raised then.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_FILENAME = ".catalog.lock"


class CatalogLockTimeout(TimeoutError):
    """Raised when the catalog lock could not be acquired in time."""


@contextmanager
def catalog_lock(catalog_dir: Path, *, timeout: float = 60.0, poll: float = 0.05) -> Iterator[None]:
    """Hold the exclusive catalog lock for the duration of the block.

    ``catalog_dir`` is the directory holding ``catalog.csv`` (created if
    missing). ``timeout`` is the maximum seconds to wait for another holder.
    """
    catalog_dir = Path(catalog_dir)
    catalog_dir.mkdir(parents=True, exist_ok=True)
    lock_path = catalog_dir / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CatalogLockTimeout(
                        f"could not acquire {lock_path} within {timeout:g}s "
                        "(another catalog writer is running)"
                    ) from None
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
