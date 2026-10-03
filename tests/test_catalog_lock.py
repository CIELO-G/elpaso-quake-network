"""lib.catalog_lock — cross-process exclusion for the catalog files."""

from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from lib.catalog_lock import LOCK_FILENAME, CatalogLockTimeout, catalog_lock


def _hold_lock(catalog_dir: str, hold_s: float, started) -> None:
    with catalog_lock(catalog_dir, timeout=5):
        started.set()
        time.sleep(hold_s)


def test_lock_creates_sentinel_and_releases(tmp_path):
    with catalog_lock(tmp_path, timeout=1):
        assert (tmp_path / LOCK_FILENAME).exists()
    # Re-acquirable immediately after release.
    with catalog_lock(tmp_path, timeout=0.2):
        pass


def test_other_process_blocks_then_times_out(tmp_path):
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(tmp_path), 1.5, started))
    child.start()
    try:
        assert started.wait(10), "child never acquired the lock"
        t0 = time.monotonic()
        with pytest.raises(CatalogLockTimeout):
            with catalog_lock(tmp_path, timeout=0.3):
                pass
        assert time.monotonic() - t0 >= 0.3
        # Once the child releases, we get in.
        with catalog_lock(tmp_path, timeout=5):
            pass
    finally:
        child.join(10)


def test_lock_released_when_holder_dies(tmp_path):
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(tmp_path), 30, started))
    child.start()
    assert started.wait(10)
    child.kill()
    child.join(10)
    # flock dies with the process: no stale lock.
    with catalog_lock(tmp_path, timeout=2):
        pass
