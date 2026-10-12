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
            "elevation_m": 1200.0, "start_date": "2025-01-01",
            "channels": "EH?", "instrument": {"geophone": {"sensitivity": 3.9965e8}}}
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


# ── B2: instrument / channel layout validated at load time ─────────────────

from lib.models import channel_layout, velocity_sensor  # noqa: E402


def test_velocity_sensor_prefers_geophone_over_accelerometer():
    e = _entry(model="RS4D", channels="E??", instrument={
        "accelerometer": {"sensitivity": 384500.0},
        "geophone": {"sensitivity": 399650000.0},
    })
    kind, spec = velocity_sensor(e)
    assert kind == "geophone" and spec["sensitivity"] == 399650000.0
    assert channel_layout(e) == ("EH", ["Z"])


def test_missing_instrument_fails_at_load_with_station_name():
    with pytest.raises(StationValidationError, match=r"\(X\).*instrument"):
        validate_stations([_entry(instrument={})])
    with pytest.raises(StationValidationError, match="no velocity sensor"):
        validate_stations([_entry(instrument={"accelerometer": {"sensitivity": 1.0}})])
    with pytest.raises(StationValidationError, match="sensitivity must be a positive number"):
        validate_stations([_entry(instrument={"geophone": {"sensitivity": "big"}})])


def test_channel_layouts():
    assert channel_layout(_entry(channels="HH?", instrument={"seismometer": {"sensitivity": 1.0}})) == ("HH", ["E", "N", "Z"])
    assert channel_layout(_entry(channels="EH?")) == ("EH", ["E", "N", "Z"])
    assert channel_layout(_entry(channels="EHZ", model="RS1D")) == ("EH", ["Z"])
    with pytest.raises(StationValidationError, match="channel layout"):
        channel_layout(_entry(channels="XYZ"))


def test_real_stations_build_phasenet_dict_unchanged():
    """Same PhaseNet station dict the old hard-coded branches produced."""
    import json
    spec = importlib.util.spec_from_file_location("detect_stage_b2", ROOT / "3-detection/detect.py")
    mod = importlib.util.module_from_spec(spec); sys.modules["detect_stage_b2"] = mod
    spec.loader.exec_module(mod)
    stations = json.loads((ROOT / "stations.json").read_text())
    pn = mod.build_phasenet_stations(stations)
    assert len(pn) == 14
    for s in stations:
        key = f"{s['network']}.{s['station']}.{s.get('location','00')}." + ("HH" if s["channels"] == "HH?" else "EH")
        comp = pn[key]["component"]
        if s["model"] == "RS4D":
            assert comp == ["Z"] and pn[key]["response"] == [s["instrument"]["geophone"]["sensitivity"]]
        elif s["channels"] == "HH?":
            assert comp == ["E", "N", "Z"] and pn[key]["response"] == [s["instrument"]["seismometer"]["sensitivity"]] * 3
        else:
            assert comp == ["E", "N", "Z"] and pn[key]["response"] == [s["instrument"]["geophone"]["sensitivity"]] * 3


def test_node_channel_layout():
    e = _entry(channels="GP?", model="node", instrument={"node": {"sensitivity": 1.5e9}})
    assert channel_layout(e) == ("GP", ["1", "2", "Z"])
    assert velocity_sensor(e)[0] == "node"


def test_station_active_between():
    from lib.models import station_active_between
    e = _entry(end_date="2026-02-23")  # start 2025-01-01
    assert station_active_between(e, date(2026, 2, 1), date(2026, 3, 1))
    assert not station_active_between(e, date(2026, 3, 1), date(2026, 3, 2))
    assert not station_active_between(e, date(2024, 1, 1), date(2024, 12, 31))
    assert station_active_between(_entry(), date(2030, 1, 1), date(2030, 1, 2))
