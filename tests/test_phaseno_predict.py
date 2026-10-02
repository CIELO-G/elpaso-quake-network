"""Plumbing + numerics-contract tests for 3-detection/phaseno/predict.py.
No torch needed (the model is imported lazily); model tests live in the
phaseno env."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "phaseno_predict", ROOT / "3-detection" / "phaseno" / "predict.py")
pp = importlib.util.module_from_spec(spec)
sys.modules["phaseno_predict"] = pp      # dataclasses need the module registered
spec.loader.exec_module(pp)


# ── numerics contracts with the notebook ─────────────────────────────
def test_highpass_equals_obspy_causal_filter():
    """The notebook filters with obspy Trace.filter('highpass', freq=1):
    a causal 4-pole Butterworth. A zero-phase filter here changed a
    third of the picks in review — this pins the contract."""
    from obspy import Trace

    rng = np.random.default_rng(0)
    x = rng.normal(size=6000)
    tr = Trace(x.copy()); tr.stats.sampling_rate = pp.FS
    tr.filter("highpass", freq=1.0)                 # obspy default zerophase=False
    ours = pp.highpass(x[None, :], 1.0)[0]
    assert np.max(np.abs(ours - tr.data)) < 1e-6


def test_detect_peaks_is_upstream_semantics():
    x = np.zeros(1000)
    x[100] = 0.9; x[200] = 0.8       # exactly mpd apart -> upstream keeps the higher only
    x[400] = 0.35; x[700] = 0.5
    idx, hts = pp.extract_picks(x, threshold=0.4, mpd=100)
    assert list(idx) == [100, 700] and np.allclose(hts, [0.9, 0.5])
    plateau = np.zeros(300); plateau[100:103] = 0.9
    assert list(pp.extract_picks(plateau, 0.4, 10)[0]) == [100]   # rising edge
    edge = np.zeros(300); edge[0] = 0.9; edge[-1] = 0.9
    assert len(pp.extract_picks(edge, 0.4, 10)[0]) == 0             # never index 0 / n-1


def test_normalize_coords_matches_upstream_formula():
    coords = pp.normalize_coords([-106.4, -106.9, -105.9], [31.8, 31.3, 32.3],
                                 center=(-106.4, 31.8))
    assert np.allclose(coords, [[0.5, 0.5], [0.25, 0.25], [0.75, 0.75]])
    assert coords.dtype == np.float32
    with pytest.raises(ValueError):
        pp.normalize_coords([-106.4, -104.0], [31.8, 31.8], center=(-106.4, 31.8))


def test_center_is_fixed_from_all_stations():
    stations = [{"longitude": -106.0, "latitude": 31.0},
                {"longitude": -107.0, "latitude": 32.0}]
    assert pp.network_center(stations) == (-106.5, 31.5)


def test_build_edges_full_graph_and_distance_cut():
    coords = np.array([[0.5, 0.5], [0.6, 0.5], [0.9, 0.5]], dtype=np.float32)
    assert pp.build_edges(coords, 1000).shape == (6, 9)
    near = pp.build_edges(coords, 25)                # 0.1 units = 22.2 km
    pairs = set(zip(near[0].astype(int), near[1].astype(int)))
    assert pairs == {(0, 0), (1, 1), (2, 2), (0, 1), (1, 0)}
    k = list(zip(near[0].astype(int), near[1].astype(int))).index((0, 1))
    assert np.allclose(near[2:, k], [0.5, 0.5, 0.6, 0.5])


# ── channel selection mirrors detect.py ──────────────────────────────
def test_station_channels_mirror_detect_rules():
    assert pp.station_channels({"model": "RS4D"}) == ("EH", ["Z"])
    assert pp.station_channels({"channels": "HH?"}) == ("HH", ["E", "N", "Z"])
    assert pp.station_channels({"model": "RS3D"}) == ("EH", ["E", "N", "Z"])


def _write_day(tmp_path, sta, chans, t0, n=360000, seed=1, model=None):
    from obspy import Stream, Trace
    rng = np.random.default_rng(seed)
    day = tmp_path / "2026" / "100"; day.mkdir(parents=True, exist_ok=True)
    for cha in chans:
        tr = Trace(rng.normal(size=n).astype(np.float32))
        tr.stats.update({"network": "AM", "station": sta, "location": "00",
                         "channel": cha, "sampling_rate": 100, "starttime": t0})
        Stream([tr]).write(str(day / f"AM.{sta}.00.{cha}.2026.100.mseed"), format="MSEED")
    return day


def test_rs4d_uses_ehz_only_and_duplicates_vertical(tmp_path):
    from obspy import UTCDateTime
    t0 = UTCDateTime(year=2026, julday=100)
    day = _write_day(tmp_path, "R9070", ["EHZ", "ENE", "ENN", "ENZ"], t0)
    st = [{"network": "AM", "station": "R9070", "model": "RS4D",
           "longitude": -106.4, "latitude": 31.8,
           "instrument": {"geophone": {"sensitivity": 1.0}}}]
    sds = pp.load_day(day, st, t0, t0 + 600, pp.Params())
    assert len(sds) == 1 and sds[0].ncomp == 1 and sds[0].chan_prefix == "EH"
    assert np.array_equal(sds[0].data[0], sds[0].data[2])        # E := Z
    assert np.array_equal(sds[0].data[1], sds[0].data[2])        # N := Z


def test_late_starting_trace_is_not_phantom_padded(tmp_path):
    """A trace whose sample grid starts 9 ms after the window must land
    with its own first sample at index 0 (no leading pad / 10-ms shift)."""
    from obspy import Stream, Trace, UTCDateTime
    t0 = UTCDateTime(year=2026, julday=100)
    day = tmp_path / "2026" / "100"; day.mkdir(parents=True)
    x = np.arange(1, 60001, dtype=np.float32)          # 1, 2, 3, ...
    tr = Trace(x); tr.stats.update({"network": "AM", "station": "AAA", "location": "00",
                                    "channel": "EHZ", "sampling_rate": 100,
                                    "starttime": t0 + 0.009})
    Stream([tr]).write(str(day / "AM.AAA.00.EHZ.2026.100.mseed"), format="MSEED")
    st = [{"network": "AM", "station": "AAA", "model": "RS4D",
           "longitude": -106.4, "latitude": 31.8}]
    sd = pp.load_day(day, st, t0, t0 + 100, pp.Params())[0]
    assert sd.valid[0] and sd.data[2, 0] == 1.0 and sd.data[2, 1] == 2.0


def test_multi_segment_file_is_merged_not_truncated(tmp_path):
    from obspy import Stream, Trace, UTCDateTime
    t0 = UTCDateTime(year=2026, julday=100)
    day = tmp_path / "2026" / "100"; day.mkdir(parents=True)
    rng = np.random.default_rng(2)
    a = Trace(rng.normal(size=60000).astype(np.float32))
    b = Trace(rng.normal(size=60000).astype(np.float32))
    for tr, start in ((a, t0), (b, t0 + 700)):
        tr.stats.update({"network": "AM", "station": "AAA", "location": "00",
                         "channel": "EHZ", "sampling_rate": 100, "starttime": start})
    Stream([a, b]).write(str(day / "AM.AAA.00.EHZ.2026.100.mseed"), format="MSEED")
    st = [{"network": "AM", "station": "AAA", "model": "RS4D",
           "longitude": -106.4, "latitude": 31.8}]
    sd = pp.load_day(day, st, t0, t0 + 1300, pp.Params())[0]
    v = sd.valid
    assert v[:60000].all() and v[70000:130000].all() and not v[60000:70000].any()


def test_zero_runs_flag_ingest_zero_fill():
    x = np.ones(1000); x[200:400] = 0.0; x[500:520] = 0.0
    m = pp.zero_runs(x, min_len=100)
    assert m[200:400].all() and not m[500:520].any() and not m[:200].any()


def test_gap_guard_and_preprocess_keep_gaps_silent():
    valid = np.ones(50, bool); valid[20:25] = False
    ok = pp.gap_guard(valid, 3)
    assert not ok[17:28].any() and ok[:17].all() and ok[28:].all()
    rng = np.random.default_rng(0)
    data = rng.normal(size=(3, 4000)) + 5.0
    v = np.ones(4000, bool); v[1000:1500] = False
    x = pp.preprocess(data, v, 1.0)
    assert np.all(x[:, 1000:1500] == 0.0) and abs(x[:, v].mean()) < 0.1


def test_amplitude_and_sensitivity():
    vel = np.zeros((3, 2000)); vel[2, 150] = 3.0; vel[0, 900] = 9.0
    assert pp.amplitude_at(vel, 100, None, 8.0) == 3.0
    vel[1, 300] = 5.0
    assert pp.amplitude_at(vel, 100, 250, 8.0) == 3.0
    assert pp.station_sensitivity({"instrument": {"geophone": {"sensitivity": 3.6e8}}}) == 3.6e8
    assert pp.station_sensitivity({}) == 1.0


def test_dedup_across_seams_keeps_higher_probability():
    picks = [
        {"station": "A", "phase": "P", "time": "2026-04-10T01:00:00.100", "probability": "0.5"},
        {"station": "A", "phase": "P", "time": "2026-04-10T01:00:00.600", "probability": "0.9"},
        {"station": "A", "phase": "P", "time": "2026-04-10T01:00:05.000", "probability": "0.4"},
        {"station": "B", "phase": "P", "time": "2026-04-10T01:00:00.100", "probability": "0.4"},
    ]
    out = pp.dedup_across_seams(picks, mpd_s=1.0)
    assert [(p["station"], p["probability"]) for p in out] == \
        [("B", "0.4"), ("A", "0.9"), ("A", "0.4")]


def test_csv_schema_matches_detect_py():
    spec2 = importlib.util.spec_from_file_location("detect", ROOT / "3-detection" / "detect.py")
    det = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(det)
    assert pp.CSV_COLUMNS == det.CSV_COLUMNS


# ── day driver ────────────────────────────────────────────────────────
def _stub(monkeypatch, pick_at=(1000, 2000)):
    class Stub: pass
    monkeypatch.setattr(pp, "load_model", lambda params: (Stub(), "cpu"))
    def fake_predict(model, dev, X, coords, edges):
        prob = np.zeros((X.shape[0], 2, X.shape[2]), np.float32)
        prob[:, 0, pick_at[0]] = 0.9; prob[:, 1, pick_at[1]] = 0.8
        return prob
    monkeypatch.setattr(pp, "predict_chunk", fake_predict)


def _two_station_day(tmp_path, seconds=7200):
    from obspy import UTCDateTime
    t0 = UTCDateTime(year=2026, julday=100)
    for sta, seed in (("AAA", 1), ("BBB", 2)):
        _write_day(tmp_path, sta, ["EHE", "EHN", "EHZ"], t0, n=seconds * 100, seed=seed)
    stations = [{"network": "AM", "station": "AAA", "location": "00",
                 "longitude": -106.4, "latitude": 31.8, "model": "RS3D"},
                {"network": "AM", "station": "BBB", "location": "00",
                 "longitude": -106.5, "latitude": 31.9, "model": "RS3D"}]
    return tmp_path / "2026" / "100", stations


def test_process_day_end_to_end_with_stub_model(tmp_path, monkeypatch):
    day, stations = _two_station_day(tmp_path)
    _stub(monkeypatch)
    out = tmp_path / "picks.csv"
    n = pp.process_day(day, stations, out, pp.Params())
    rows = list(csv.DictReader(open(out)))
    assert n == len(rows) and list(rows[0].keys()) == pp.CSV_COLUMNS
    assert {r["model"] for r in rows} == {pp.MODEL_TAG}
    assert {r["station"] for r in rows} == {"AAA", "BBB"}
    assert all(r["time"].startswith("2026-04-10") and len(r["time"]) == 23 for r in rows)
    # stub fires at +10 s of every padded load; chunk 2 loads from 00:59:20, so its
    # +10 s pick lands in the pad and must be dropped by the boundary filter
    assert not any(r["time"].startswith("2026-04-10T00:59") for r in rows)


def test_missing_day_dir_raises_and_writes_nothing(tmp_path, monkeypatch):
    _stub(monkeypatch)
    out = tmp_path / "picks.csv"
    with pytest.raises(pp.DayError):
        pp.process_day(tmp_path / "2026" / "999", [], out, pp.Params())
    assert not out.exists()


def test_single_station_chunks_are_skipped_not_crashed(tmp_path, monkeypatch):
    from obspy import UTCDateTime
    t0 = UTCDateTime(year=2026, julday=100)
    day = _write_day(tmp_path, "AAA", ["EHZ"], t0, n=360000)
    stations = [{"network": "AM", "station": "AAA", "model": "RS4D",
                 "longitude": -106.4, "latitude": 31.8}]
    _stub(monkeypatch)
    with pytest.raises(pp.DayError, match="no station data"):
        pp.process_day(day, stations, tmp_path / "p.csv", pp.Params())


def test_pad_must_align_with_window_step(tmp_path, monkeypatch):
    day, stations = _two_station_day(tmp_path, seconds=100)
    _stub(monkeypatch)
    with pytest.raises(pp.DayError, match="multiple"):
        pp.process_day(day, stations, tmp_path / "p.csv", pp.Params(pad_s=30.0))
