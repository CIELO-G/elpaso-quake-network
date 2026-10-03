"""A10/A11: atomic stage outputs; raw gaps stored as gaps, not zeros."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime, read

from lib.atomic_io import atomic_replace, atomic_write_rows, atomic_write_stream

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _tr(npts, t0, cha="EHZ", rate=100.0):
    return Trace(np.arange(npts, dtype=np.int32) + 1,
                 header={"network": "AM", "station": "R0F2D", "location": "00", "channel": cha,
                         "sampling_rate": rate, "starttime": UTCDateTime(t0)})


def test_atomic_replace_leaves_target_untouched_on_error(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old")
    with pytest.raises(RuntimeError):
        with atomic_replace(target) as tmp:
            tmp.write_text("new")
            raise RuntimeError("boom")
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]  # temp cleaned up
    with atomic_replace(target) as tmp:
        tmp.write_text("new")
    assert target.read_text() == "new"


def test_atomic_write_rows_and_stream(tmp_path):
    p = tmp_path / "a" / "b.csv"
    atomic_write_rows(p, [{"x": 1, "y": "z"}], ["x", "y"])
    assert p.read_text().splitlines() == ["x,y", "1,z"]
    atomic_write_rows(p, [], ["x", "y"])
    assert p.read_text().splitlines() == ["x,y"]
    m = tmp_path / "s.mseed"
    atomic_write_stream(Stream([_tr(100, 0)]), m, format="MSEED")
    assert read(str(m))[0].stats.npts == 100
    assert not list(tmp_path.glob("*.tmp"))


def test_ingest_save_keeps_gap_and_merges_without_zeros(tmp_path):
    ingest = _load("ingest_a11", "1-ingestion/ingest.py")
    log = logging.getLogger("t")
    day = UTCDateTime(2026, 9, 27)
    # two chunks of the same hour-day with a 10-minute hole between them
    first = Stream([_tr(100 * 600, day)])                      # 00:00–00:10
    second = Stream([_tr(100 * 600, day + 1200)])              # 00:20–00:30
    ingest.save_waveforms(first, str(tmp_path), log)
    ingest.save_waveforms(second, str(tmp_path), log)
    files = list((tmp_path / "1-raw").rglob("*.mseed"))
    assert len(files) == 1
    st = read(str(files[0]))
    assert len(st) == 2                                        # gap kept as two segments
    assert all((tr.data != 0).all() for tr in st)              # no zero fill anywhere
    assert st[1].stats.starttime == day + 1200
    gaps = st.get_gaps()
    assert len(gaps) == 1 and abs(gaps[0][6] - 600) < 0.02


def test_ingest_save_is_atomic(tmp_path, monkeypatch):
    ingest = _load("ingest_a10", "1-ingestion/ingest.py")
    log = logging.getLogger("t")
    day = UTCDateTime(2026, 9, 27)
    ingest.save_waveforms(Stream([_tr(1000, day)]), str(tmp_path), log)
    f = next((tmp_path / "1-raw").rglob("*.mseed"))
    before = f.read_bytes()

    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Stream, "write", boom)
    with pytest.raises(OSError):
        ingest.save_waveforms(Stream([_tr(1000, day + 100)]), str(tmp_path), log)
    assert f.read_bytes() == before
    assert not list(f.parent.glob("*.tmp"))


def test_associate_writes_assignments_before_events(tmp_path):
    assoc = _load("assoc_a10", "4-association/associate.py")
    order = []
    log = logging.getLogger("t")
    orig = assoc.atomic_write_rows

    def spy(path, rows, cols):
        order.append(Path(path).name)
        orig(path, rows, cols)
    assoc.atomic_write_rows = spy
    src = (ROOT / "4-association" / "associate.py").read_text()
    # every call pair in main() must be assignments then events
    import re
    pairs = re.findall(r"write_(catalog|assignments)_csv\(.*?\)\n\s*write_(catalog|assignments)_csv", src)
    assert pairs and all(p == ("assignments", "catalog") for p in pairs), pairs
    assoc.write_assignments_csv([], tmp_path / "a.csv", log)
    assoc.write_catalog_csv([], tmp_path / "e.csv", log)
    assert order == ["a.csv", "e.csv"]
