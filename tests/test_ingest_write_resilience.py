"""One corrupt chunk must not kill the ingest run (2026-10-06 incident).

Raspberry Shake served an hour of AM.R0F2D whose Steim2 integrity check
failed; re-encoding the merged day-file raised a bare Exception from
libmseed ("Unable to represent difference in <= 30 bits") that escaped the
worker thread and aborted ingest for all 14 stations on every retry.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime, read

from lib.db import DownloadDB

ROOT = Path(__file__).resolve().parents[1]


def _load_ingest():
    spec = importlib.util.spec_from_file_location("ingest_stage_res", ROOT / "1-ingestion/ingest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_stage_res"] = mod
    spec.loader.exec_module(mod)
    return mod


ingest = _load_ingest()


def _trace(data, start="2026-10-06T02:00:00", sta="R0F2D", cha="EHN"):
    tr = Trace(data=np.asarray(data))
    tr.stats.network = "AM"
    tr.stats.station = sta
    tr.stats.location = "00"
    tr.stats.channel = cha
    tr.stats.sampling_rate = 100.0
    tr.stats.starttime = UTCDateTime(start)
    return tr


def _unencodable_int32():
    # Consecutive differences wider than Steim2's 30-bit limit.
    data = np.zeros(2000, dtype=np.int32)
    data[500] = 2**31 - 1
    data[501] = -(2**31) + 1
    return data


def test_steim2_really_fails_on_this_data(tmp_path):
    st = Stream([_trace(_unencodable_int32())])
    with pytest.raises(Exception, match="30 bits|packing"):
        st.write(str(tmp_path / "x.mseed"), format="MSEED")


def test_save_waveforms_keeps_samples_via_fallback_encoding(tmp_path):
    data = _unencodable_int32()
    st = Stream([_trace(data)])
    logger = logging.getLogger("test-fallback")

    paths = ingest.save_waveforms(st, str(tmp_path), logger)

    assert len(paths) == 1
    back = read(paths[0])
    assert len(back) == 1
    np.testing.assert_array_equal(back[0].data, data)
    assert back[0].stats.mseed.encoding == "INT32"


def test_fallback_merges_into_existing_steim2_day_file(tmp_path):
    logger = logging.getLogger("test-fallback-merge")
    good = Stream([_trace(np.arange(1000, dtype=np.int32), start="2026-10-06T01:00:00")])
    ingest.save_waveforms(good, str(tmp_path), logger)

    bad = Stream([_trace(_unencodable_int32(), start="2026-10-06T02:00:00")])
    paths = ingest.save_waveforms(bad, str(tmp_path), logger)

    back = read(paths[0])
    assert sum(tr.stats.npts for tr in back) == 3000
    assert back[0].stats.starttime == UTCDateTime("2026-10-06T01:00:00")


def test_fallback_handles_float_data(tmp_path):
    logger = logging.getLogger("test-fallback-float")
    st = Stream([_trace(np.array([0.0, 1e12, -1e12, 0.0] * 100))])
    ingest.write_day_file(st, tmp_path / "f.mseed", logger)
    back = read(str(tmp_path / "f.mseed"))
    assert back[0].stats.mseed.encoding in ("FLOAT64", "STEIM2")
    assert back[0].stats.npts == 400


def test_non_encoding_write_error_still_raises(tmp_path):
    def boom(*a, **k):
        raise PermissionError("disk is read-only")

    logger = logging.getLogger("test-other-error")
    st = Stream([_trace(np.arange(10, dtype=np.int32))])
    orig = ingest.atomic_write_stream
    ingest.atomic_write_stream = boom
    try:
        with pytest.raises(PermissionError):
            ingest.write_day_file(st, tmp_path / "p.mseed", logger)
    finally:
        ingest.atomic_write_stream = orig


class _FakeClient:
    pass


def _station_run(tmp_path, monkeypatch, *, fake_fetch, fake_save, t0, t1, delay=0, db=None):
    db = db or DownloadDB(str(tmp_path / "t.db"))
    logger = logging.getLogger("test-chunk-guard")
    sleeps: list[float] = []
    monkeypatch.setattr(ingest, "fetch_waveforms", fake_fetch)
    monkeypatch.setattr(ingest, "save_waveforms", fake_save)
    monkeypatch.setattr(ingest, "fetch_station_metadata", lambda *a, **k: _inv())
    monkeypatch.setattr(ingest, "expected_channels", lambda *a, **k: {"EHN"})
    monkeypatch.setattr(ingest, "chunk_coverage", lambda *a, **k: 1.0)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: sleeps.append(s))
    config = {
        "default_location": "00", "default_channels": "EH?", "chunk_hours": 1,
        "min_chunk_coverage": 0.98, "refetch_window_days": 7,
        "station_delay_seconds": delay, "output_dir": str(tmp_path),
    }
    counts = ingest.process_station(
        _FakeClient(), db, {"network": "AM", "station": "R0F2D"},
        UTCDateTime(t0), UTCDateTime(t1), config, logger,
    )
    return counts, db, sleeps


def _status(db, t0, t1):
    return db.chunk_status("AM", "R0F2D", "00", "EH?", UTCDateTime(t0), UTCDateTime(t1))[0]


def test_write_failure_marks_only_that_day_failed_and_run_continues(tmp_path, monkeypatch):
    """One bad day-file write records its chunks failed; the next day still succeeds."""
    calls = {"n": 0}

    def fake_fetch(client, net, sta, loc, cha, t0, t1, config, log):
        return Stream([_trace(np.arange(100, dtype=np.int32), start=str(t0))])

    def fake_save(st, output_dir, log):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("msr_pack(AM_R0F2D_00_EHN_D): Error packing data samples")
        return ["ok.mseed"]

    counts, db, _ = _station_run(
        tmp_path, monkeypatch, fake_fetch=fake_fetch, fake_save=fake_save,
        t0="2026-10-06T22:00:00", t1="2026-10-07T02:00:00",
    )
    assert calls["n"] == 2  # one write per station-day, not per chunk
    assert counts["failed"] == 2 and counts["success"] == 2
    assert _status(db, "2026-10-06T22:00:00", "2026-10-06T23:00:00") == "failed"
    assert _status(db, "2026-10-07T00:00:00", "2026-10-07T01:00:00") == "success"


def test_tracker_rows_written_only_after_day_file(tmp_path, monkeypatch):
    """Chunks are 'success' in the tracker only once their day-file write returned."""
    db = DownloadDB(str(tmp_path / "t.db"))
    seen_at_write: list = []

    def fake_fetch(client, net, sta, loc, cha, t0, t1, config, log):
        return Stream([_trace(np.arange(100, dtype=np.int32), start=str(t0))])

    def fake_save(st, output_dir, log):
        seen_at_write.append(db.chunk_status("AM", "R0F2D", "00", "EH?",
                                             UTCDateTime("2026-10-06T00:00:00"),
                                             UTCDateTime("2026-10-06T01:00:00")))
        assert len(st) == 3  # all three hourly chunks arrive in one write
        return ["ok.mseed"]

    counts, _, _ = _station_run(
        tmp_path, monkeypatch, fake_fetch=fake_fetch, fake_save=fake_save,
        t0="2026-10-06T00:00:00", t1="2026-10-06T03:00:00", db=db,
    )
    assert counts["success"] == 3
    assert seen_at_write == [None]  # nothing recorded before the write
    assert _status(db, "2026-10-06T00:00:00", "2026-10-06T01:00:00") == "success"


def test_no_sleep_after_empty_reply(tmp_path, monkeypatch):
    def fake_fetch(client, net, sta, loc, cha, t0, t1, config, log):
        if t0.hour == 1:
            return Stream()
        return Stream([_trace(np.arange(100, dtype=np.int32), start=str(t0))])

    counts, _, sleeps = _station_run(
        tmp_path, monkeypatch, fake_fetch=fake_fetch, fake_save=lambda *a: ["ok"],
        t0="2026-10-06T00:00:00", t1="2026-10-06T04:00:00", delay=3,
    )
    # chunks 0,1,2,3: sleep after 0 (data), none after 1 (empty), after 2 (data), none after 3 (last)
    assert counts["success"] == 3 and counts["no_data"] == 1
    assert sleeps == [3, 3]


def _inv():
    from obspy.core.inventory import Channel, Inventory, Network, Station
    ch = Channel(code="EHN", location_code="00", latitude=0, longitude=0, elevation=0, depth=0)
    sta = Station(code="R0F2D", latitude=0, longitude=0, elevation=0, channels=[ch])
    return Inventory(networks=[Network(code="AM", stations=[sta])], source="test")
