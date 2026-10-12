"""Retiring a station: end_date keeps history but stops expectations."""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import date
from pathlib import Path

import pytest
from obspy import UTCDateTime

from lib.models import Station, StationValidationError, station_active_on, validate_stations

ROOT = Path(__file__).resolve().parents[1]


def _entry(**kw):
    base = {"network": "AM", "station": "X", "latitude": 31.8, "longitude": -106.4,
            "elevation_m": 1200.0, "start_date": "2025-01-01"}
    base.update(kw)
    return base


def test_station_active_window():
    e = _entry(end_date="2026-02-23")
    assert not station_active_on(e, date(2024, 12, 31))
    assert station_active_on(e, date(2025, 1, 1))
    assert station_active_on(e, date(2026, 2, 23))
    assert not station_active_on(e, date(2026, 2, 24))
    assert station_active_on(_entry(), date(2030, 1, 1))
    assert station_active_on(_entry(end_date=""), date(2030, 1, 1))


def test_validation_rejects_bad_dates():
    with pytest.raises(StationValidationError, match="end_date"):
        validate_stations([_entry(end_date="2026/02/23")])
    with pytest.raises(StationValidationError, match="before start_date"):
        validate_stations([_entry(end_date="2024-01-01")])
    with pytest.raises(StationValidationError, match="start_date is required"):
        validate_stations([_entry(start_date="")])
    assert validate_stations([_entry(end_date="2026-02-23")]) == []


def test_station_model_carries_end_date():
    st = Station.from_dict(_entry(end_date="2026-02-23"))
    assert st.end_date == "2026-02-23"
    assert Station.from_dict(_entry()).end_date == ""


def test_real_stations_file_validates_and_r5912_is_retired():
    import json
    stations = json.loads((ROOT / "stations.json").read_text())
    validate_stations(stations)
    r = next(s for s in stations if s["station"] == "R5912")
    assert r["end_date"] == "2026-02-23"
    assert not station_active_on(r, date(2026, 10, 1))


def _load_ingest():
    spec = importlib.util.spec_from_file_location("ingest_stage_end", ROOT / "1-ingestion/ingest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_stage_end"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ingest_skips_retired_station_window(monkeypatch):
    ingest = _load_ingest()
    called = {"meta": 0}
    monkeypatch.setattr(ingest, "fetch_station_metadata", lambda *a, **k: called.__setitem__("meta", 1))
    cfg = {"default_location": "00", "default_channels": "EH?"}
    counts = ingest.process_station(
        None, None, {"network": "AM", "station": "R5912", "start_date": "2022-12-03", "end_date": "2026-02-23"},
        UTCDateTime("2026-10-06"), UTCDateTime("2026-10-07"), cfg, logging.getLogger("t"),
    )
    assert counts["success"] == 0 and called["meta"] == 0
