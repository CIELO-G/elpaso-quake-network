"""B6: PhaseNet input is split into station groups; picks do not depend on it."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import numpy as np
from obspy import Stream, Trace, UTCDateTime, read

ROOT = Path(__file__).resolve().parents[1]


def _load_detect():
    spec = importlib.util.spec_from_file_location("detect_stage_b6", ROOT / "3-detection/detect.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["detect_stage_b6"] = mod
    spec.loader.exec_module(mod)
    return mod


detect = _load_detect()


def _files(stations, chans=("EHE", "EHN", "EHZ")):
    return [Path(f"AM.{s}.00.{c}.2026.284.mseed") for s in stations for c in chans]


def test_group_keeps_a_station_together_and_bounds_size():
    files = _files(["R0F2D", "R3B56", "R3E95", "R4B41", "RDB14"]) + [Path("EP.KIDD..HHZ.2026.284.mseed")]
    groups = detect.group_raw_files(files, 4)
    assert [len(g) for g in groups] == [12, 4]  # AM.R0F2D..R4B41 (3 ch each), then AM.RDB14 + EP.KIDD
    for g in groups:
        stations = {".".join(f.name.split(".")[:2]) for f in g}
        assert len(stations) <= 4
    all_stations = [".".join(f.name.split(".")[:2]) for g in groups for f in g]
    assert sorted(set(all_stations)) == ["AM.R0F2D", "AM.R3B56", "AM.R3E95", "AM.R4B41", "AM.RDB14", "EP.KIDD"]


def test_group_size_zero_means_one_file():
    files = _files(["A", "B", "C"])
    assert len(detect.group_raw_files(files, 0)) == 1
    assert detect.group_raw_files([], 6) == []


def _write(tmp_path, sta, t0, npts):
    tr = Trace(data=np.arange(npts, dtype=np.int32))
    tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel = "AM", sta, "00", "EHZ"
    tr.stats.sampling_rate = 100.0
    tr.stats.starttime = UTCDateTime(t0)
    p = tmp_path / f"AM.{sta}.00.EHZ.2026.284.mseed"
    Stream([tr]).write(str(p), format="MSEED")
    return p


def test_groups_are_padded_to_the_common_day_span(tmp_path):
    """Each group file covers the same window, so PhaseNet's 30 s windows line up."""
    f1 = _write(tmp_path, "AAAA1", "2026-10-11T00:00:00", 6000)   # 00:00 - 00:01
    f2 = _write(tmp_path, "BBBB1", "2026-10-11T00:00:30", 6000)   # 00:00:30 - 00:01:30
    span = detect.day_time_span([f1, f2], logging.getLogger("t"))
    assert span[0] == UTCDateTime("2026-10-11T00:00:00") and span[1] == UTCDateTime("2026-10-11T00:01:29.99")
    out = tmp_path / "g1.mseed"
    assert detect.merge_raw_data([f2], out, logging.getLogger("t"), span=span)
    st = read(str(out))
    assert st[0].stats.starttime == span[0] and st[0].stats.endtime == span[1]
    assert st[0].data[:3000].max() == 0  # zero-padded before the station's first sample
