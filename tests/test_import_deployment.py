"""scripts/import_deployment.py: clock snapping, decimation, integer counts, duplicate files."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime, read

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("import_deployment_mod", ROOT / "scripts/import_deployment.py")
imp = importlib.util.module_from_spec(spec)
sys.modules["import_deployment_mod"] = imp
spec.loader.exec_module(imp)


def _tr(data, rate, t0="2021-06-05T07:00:00.000998", sta="103", cha="GPZ"):
    tr = Trace(data=np.asarray(data))
    tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel = "4B", sta, "", cha
    tr.stats.sampling_rate = rate
    tr.stats.starttime = UTCDateTime(t0)
    return tr


def test_snap_rate_and_decimate_integer_counts():
    n = 100_000
    t = np.arange(n) / 1000.0
    data = np.round(1000 * np.sin(2 * np.pi * 5 * t)).astype(np.float32)  # 5 Hz, integer counts
    tr = _tr(data, 999.9999389648438)
    integral = imp.normalise_trace(tr, target_rate=100.0)
    assert integral and tr.data.dtype == np.int32
    assert tr.stats.sampling_rate == 100.0 and tr.stats.npts == n // 10
    # a 5 Hz sine survives decimation to 100 Hz with its amplitude
    assert abs(np.abs(tr.data).max() - 1000) <= 20


def test_float_data_stays_float():
    tr = _tr(np.random.default_rng(0).normal(size=5000).astype(np.float64), 100.0)
    assert imp.normalise_trace(tr, target_rate=100.0) is False
    assert tr.data.dtype == np.float32


def test_non_integer_factor_rejected():
    tr = _tr(np.zeros(1000, dtype=np.float32), 250.0)
    with pytest.raises(ValueError, match="integer factor"):
        imp.normalise_trace(tr, target_rate=100.0)


def test_station_files_split_at_midnight_and_skip_duplicates(tmp_path):
    raw = tmp_path / "1-raw"
    # one "day file" from 23:00 to 01:00 next day at 100 Hz, written twice under two names
    data = np.arange(720_000, dtype=np.float32)
    f1 = tmp_path / "a.mseed"; f2 = tmp_path / "b.mseed"
    Stream([_tr(data, 100.0, t0="2021-06-05T23:00:00")]).write(str(f1), format="MSEED")
    Stream([_tr(data, 100.0, t0="2021-06-05T23:00:00")]).write(str(f2), format="MSEED")
    st = imp.import_station_files("4B.103", [f1, f2], 100.0, raw)
    assert st["duplicates"] == 1 and st["pieces"] == 2
    d156 = read(str(raw / "2021/156/4B.103..GPZ.2021.156.mseed"))[0]
    d157 = read(str(raw / "2021/157/4B.103..GPZ.2021.157.mseed"))[0]
    assert d156.stats.npts == 360_000 and d157.stats.npts == 360_000
    assert d157.stats.starttime == UTCDateTime("2021-06-06T00:00:00")
    assert d156.stats.mseed.encoding == "STEIM2"
