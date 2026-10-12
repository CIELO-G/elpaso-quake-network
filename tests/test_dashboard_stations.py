"""B7: dashboard follows stations.json without a restart and per-station channels."""

from __future__ import annotations

import json
import os
import time

import dashboard.deps as deps
from dashboard.deps import load_stations, station_channels


def _entry(sta, **kw):
    e = {"network": "AM", "station": sta, "latitude": 31.8, "longitude": -106.4,
         "elevation_m": 1200.0, "start_date": "2025-01-01", "channels": "EH?", "model": "RS3D",
         "instrument": {"geophone": {"sensitivity": 3.9965e8}}}
    e.update(kw)
    return e


def test_reloads_when_stations_file_changes(tmp_path, monkeypatch):
    f = tmp_path / "stations.json"
    f.write_text(json.dumps([_entry("AAAA1")]))
    monkeypatch.setattr(deps, "STATIONS_FILE", f)
    monkeypatch.setattr(deps, "_stations_cache", None)
    monkeypatch.setattr(deps, "_stations_cache_mtime", None)
    assert [s["station"] for s in load_stations()] == ["AAAA1"]
    f.write_text(json.dumps([_entry("AAAA1"), _entry("BBBB1")]))
    os.utime(f, (time.time() + 5, time.time() + 5))  # guarantee a different mtime
    assert [s["station"] for s in load_stations()] == ["AAAA1", "BBBB1"]


def test_channel_codes_follow_station_layout(tmp_path, monkeypatch):
    f = tmp_path / "stations.json"
    f.write_text(json.dumps([
        _entry("RS3D1"),
        _entry("RS4D1", model="RS4D", channels="E??"),
        _entry("RS1D1", model="RS1D", channels="EHZ"),
        {"network": "EP", "station": "KIDD", "latitude": 31.77, "longitude": -106.5, "elevation_m": 1162.5,
         "start_date": "2020-01-01", "channels": "HH?", "model": "broadband",
         "instrument": {"seismometer": {"sensitivity": 1.0}}},
    ]))
    monkeypatch.setattr(deps, "STATIONS_FILE", f)
    monkeypatch.setattr(deps, "_stations_cache", None)
    monkeypatch.setattr(deps, "_stations_cache_mtime", None)
    by = {s["station"]: s for s in load_stations()}
    assert station_channels(by["RS3D1"], "3C") == ["EHZ", "EHN", "EHE"]
    assert station_channels(by["RS4D1"], "3C") == ["EHZ"]
    assert station_channels(by["RS1D1"], "3C") == ["EHZ"]
    assert station_channels(by["KIDD"], "3C") == ["HHZ", "HHN", "HHE"]
    assert station_channels(by["KIDD"], "EHZ") == ["EHZ"]  # file lookup falls back to HHZ


def test_find_mseed_falls_back_across_station_types(tmp_path):
    """The default EHZ request must find a broadband's HHZ and a node's GPZ/GP1."""
    from dashboard.routes.waveforms import _find_mseed

    for name in ("AM.R1111.00.EHZ.2021.160.mseed", "TX.KIDD.00.HHZ.2021.160.mseed",
                 "4B.101..GPZ.2021.160.mseed", "4B.101..GP1.2021.160.mseed",
                 "4B.101..GP2.2021.160.mseed"):
        (tmp_path / name).write_bytes(b"")
    names = lambda c: [p.name for p in c]  # noqa: E731
    assert names(_find_mseed(tmp_path, "AM", "R1111", "EHZ")) == ["AM.R1111.00.EHZ.2021.160.mseed"]
    assert names(_find_mseed(tmp_path, "TX", "KIDD", "EHZ")) == ["TX.KIDD.00.HHZ.2021.160.mseed"]
    assert names(_find_mseed(tmp_path, "4B", "101", "EHZ")) == ["4B.101..GPZ.2021.160.mseed"]
    assert names(_find_mseed(tmp_path, "4B", "101", "EHN")) == ["4B.101..GP1.2021.160.mseed"]
    assert names(_find_mseed(tmp_path, "4B", "101", "EHE")) == ["4B.101..GP2.2021.160.mseed"]
    assert _find_mseed(tmp_path, "4B", "999", "EHZ") == []
    assert _find_mseed(tmp_path / "missing", "4B", "101", "EHZ") == []


def test_all_station_fanout_skips_stations_not_active_that_day(tmp_path, monkeypatch):
    """A 2021 campaign window lists the nodes, not the 2025 Shake stations."""
    from obspy import UTCDateTime
    import dashboard.routes.waveforms as wf

    node = _entry("101", network="4B", channels="GP?", model="node", start_date="2021-06-05",
                  end_date="2021-06-15", instrument={"node": {"sensitivity": 1.5e9}})
    shake = _entry("R1111", start_date="2025-09-07")
    monkeypatch.setattr(wf, "load_stations", lambda: [dict(node, channel_codes=["GP1", "GP2", "GPZ"]),
                                                      dict(shake, channel_codes=["EHE", "EHN", "EHZ"])])
    seen = []
    monkeypatch.setattr(wf, "_find_mseed", lambda day_dir, net, sta, chan: seen.append((net, sta, chan)) or [])
    t0 = UTCDateTime("2021-06-11T04:45:00")
    out = wf._build_traces_for_window(tmp_path, t0, t0 + 30, channel="3C", max_samples=100, freqmin=None,
                                      freqmax=None, spectrogram=False, picks_by_station={}, include_amplitude=False)
    assert {(n, s) for n, s, _ in seen} == {("4B", "101")}
    assert all(t["network"] == "4B" for t in out)
    seen.clear()
    t1 = UTCDateTime("2026-10-01T04:45:00")
    wf._build_traces_for_window(tmp_path, t1, t1 + 30, channel="EHZ", max_samples=100, freqmin=None,
                                freqmax=None, spectrogram=False, picks_by_station={}, include_amplitude=False)
    assert {(n, s) for n, s, _ in seen} == {("AM", "R1111")}
