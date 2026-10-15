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


def test_retriage_waits_for_the_catalog_lock(tmp_path, monkeypatch, sample_stations_json):
    """`catalog.py --retriage` rewrites catalog.csv, so it must hold the lock
    like every other writer: with the lock taken it leaves the file alone."""
    import sys
    from pathlib import Path

    import pandas as pd
    import yaml

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "5-catalog"))
    import catalog

    out = tmp_path / "output"
    cat_dir = out / "5-catalog"
    cat_dir.mkdir(parents=True)
    rows = [{"event_id": "ep20260307-0000", "time": "2026-03-07T10:00:30.000", "magnitude": 1.0}]
    pd.DataFrame(rows).to_csv(cat_dir / "catalog.csv", index=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "stations_file": str(sample_stations_json),
                "input_dir": str(out),
                "output_dir": str(out),
                "log_dir": str(tmp_path / "logs"),
                "triage": {"enabled": True},
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["catalog.py", "--config", str(cfg_path), "--retriage"])
    monkeypatch.setattr(catalog, "CATALOG_LOCK_TIMEOUT_S", 0.3)

    ctx = mp.get_context("spawn")
    started = ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(cat_dir), 2.0, started))
    child.start()
    try:
        assert started.wait(10), "child never acquired the lock"
        with pytest.raises(SystemExit) as exit_info:
            catalog.main()
        assert exit_info.value.code == 3
        assert "triage" not in pd.read_csv(cat_dir / "catalog.csv").columns
    finally:
        child.join(10)

    catalog.main()  # lock free again: relabels in place
    assert "triage" in pd.read_csv(cat_dir / "catalog.csv").columns
