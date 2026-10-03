"""A6: chunk coverage, tracker migration, and the re-fetch decision."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import numpy as np
from obspy import Stream, Trace, UTCDateTime
from obspy.core.inventory import Channel, Inventory, Network, Station

from lib.db import DownloadDB

ROOT = Path(__file__).resolve().parent.parent


def _load_ingest():
    spec = importlib.util.spec_from_file_location("ingest_stage_cov", ROOT / "1-ingestion/ingest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_stage_cov"] = mod
    spec.loader.exec_module(mod)
    return mod


ingest = _load_ingest()


def _inv(codes, loc="00", rate=100.0):
    chans = [Channel(code=c, location_code=loc, latitude=0, longitude=0, elevation=0, depth=0,
                     sample_rate=rate) for c in codes]
    sta = Station(code="R5912", latitude=0, longitude=0, elevation=0, channels=chans)
    return Inventory(networks=[Network(code="AM", stations=[sta])], source="t")


def _tr(cha, npts, rate=100.0, t0=0):
    return Trace(np.zeros(npts, dtype=np.int32),
                 header={"network": "AM", "station": "R5912", "location": "00", "channel": cha,
                         "sampling_rate": rate, "starttime": UTCDateTime(t0)})


# ── expected channels from StationXML ─────────────────────────────────────

def test_expected_channels_matches_pattern_and_location():
    inv = _inv(["EHZ", "ENE", "ENN", "ENZ"])
    assert ingest.expected_channels(inv, "AM", "R5912", "00", "E??") == {
        "EHZ": 100.0, "ENE": 100.0, "ENN": 100.0, "ENZ": 100.0}
    assert set(ingest.expected_channels(inv, "AM", "R5912", "00", "EH?")) == {"EHZ"}
    assert ingest.expected_channels(inv, "AM", "R5912", "10", "E??") == {}
    assert ingest.expected_channels(inv, "AM", "NOPE", "00", "E??") == {}


# ── coverage ──────────────────────────────────────────────────────────────

def test_chunk_coverage_full_short_and_missing_channel():
    t0, t1 = UTCDateTime(0), UTCDateTime(3600)
    exp = {"EHZ": 100.0, "EHN": 100.0, "EHE": 100.0}
    full = Stream([_tr(c, 360000) for c in exp])
    assert ingest.chunk_coverage(full, exp, t0, t1) == 1.0
    # one channel has half the hour, one is missing entirely
    short = Stream([_tr("EHZ", 360000), _tr("EHN", 180000)])
    assert abs(ingest.chunk_coverage(short, exp, t0, t1) - 0.5) < 1e-6
    # gaps split into several traces still add up
    gappy = Stream([_tr(c, 200000) for c in exp] + [_tr(c, 160000, t0=2000) for c in exp])
    assert ingest.chunk_coverage(gappy, exp, t0, t1) == 1.0
    # over-delivery is capped at 1
    over = Stream([_tr(c, 400000) for c in exp])
    assert ingest.chunk_coverage(over, exp, t0, t1) == 1.0


def test_chunk_coverage_without_inventory_uses_returned_channels():
    t0, t1 = UTCDateTime(0), UTCDateTime(3600)
    st = Stream([_tr("EHZ", 360000), _tr("EHN", 90000)])
    assert abs(ingest.chunk_coverage(st, {}, t0, t1) - 0.625) < 1e-6
    assert ingest.chunk_coverage(Stream(), {}, t0, t1) == 0.0


# ── tracker migration ─────────────────────────────────────────────────────

def test_migration_adds_coverage_column_to_old_db(tmp_path):
    path = tmp_path / "1-downloads.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE downloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, network TEXT NOT NULL, station TEXT NOT NULL,
        location TEXT NOT NULL, channel TEXT NOT NULL, start_time TEXT NOT NULL,
        end_time TEXT NOT NULL, status TEXT NOT NULL, filepaths TEXT,
        download_time TEXT NOT NULL, error_message TEXT,
        UNIQUE(network, station, location, channel, start_time, end_time))""")
    con.execute("INSERT INTO downloads VALUES (NULL,'AM','R0F2D','00','EH?','a','b','success',NULL,'t',NULL)")
    con.commit(); con.close()
    db = DownloadDB(tmp_path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(downloads)")}
    assert "coverage" in cols
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == DownloadDB.SCHEMA_VERSION
    # legacy row: coverage NULL → treated as complete, never re-fetched
    assert db.chunk_status("AM", "R0F2D", "00", "EH?", "a", "b") == ("success", None)
    assert not db.needs_fetch("AM", "R0F2D", "00", "EH?", "a", "b", min_coverage=0.98)
    DownloadDB(tmp_path).close()  # re-open is idempotent
    db.close()


# ── fetch decision ────────────────────────────────────────────────────────

def test_needs_fetch_policy(tmp_path):
    db = DownloadDB(tmp_path)
    k = ("AM", "R0F2D", "00", "EH?", "s", "e")
    assert db.needs_fetch(*k, min_coverage=0.98)  # never tried
    db.record(*k, "failed", error="boom")
    assert db.needs_fetch(*k, min_coverage=0.98, within_refetch_window=False)
    db.record(*k, "success", ["f"], coverage=1.0)
    assert not db.needs_fetch(*k, min_coverage=0.98, force=True)  # complete: never again
    db.record(*k, "success", ["f"], coverage=0.5)
    assert db.needs_fetch(*k, min_coverage=0.98, within_refetch_window=True)
    assert not db.needs_fetch(*k, min_coverage=0.98, within_refetch_window=False)
    assert db.needs_fetch(*k, min_coverage=0.98, within_refetch_window=False, force=True)
    db.record(*k, "no_data")
    assert db.needs_fetch(*k, min_coverage=0.98, within_refetch_window=True)
    assert not db.needs_fetch(*k, min_coverage=0.98, within_refetch_window=False)
    assert db.short_chunks(0.98) == []
    db.record(*k, "success", ["f"], coverage=0.7)
    assert [r[4] for r in db.short_chunks(0.98)] == [0.7]
    db.close()


def test_short_chunks_make_run_partial():
    t = {"success": 300, "no_data": 0, "failed": 0, "skipped": 0, "short": 1}
    assert ingest.exit_code_for(t, 0, 14) == ingest.EXIT_PARTIAL
