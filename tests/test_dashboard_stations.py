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
