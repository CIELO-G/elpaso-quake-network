"""/api/station_health: retired stations are reported as such, not as offline."""

from __future__ import annotations

import json

from starlette.testclient import TestClient

import dashboard.deps as deps
import dashboard.routes.pipeline as pl
from dashboard.app import app


def _raw_day(raw, year, jday, stations):
    d = raw / str(year) / f"{jday:03d}"
    d.mkdir(parents=True, exist_ok=True)
    for s in stations:
        for c in ("EHE", "EHN", "EHZ"):
            (d / f"{s}.00.{c}.{year}.{jday:03d}.mseed").write_text("x")


def test_retired_station_status(tmp_path, monkeypatch):
    stations = [
        {"network": "AM", "station": "LIVE1", "latitude": 31.8, "longitude": -106.4,
         "elevation_m": 1200.0, "start_date": "2025-01-01"},
        {"network": "AM", "station": "OLD01", "latitude": 31.8, "longitude": -106.4,
         "elevation_m": 1200.0, "start_date": "2022-12-03", "end_date": "2026-02-23"},
    ]
    sf = tmp_path / "stations.json"
    sf.write_text(json.dumps(stations))
    monkeypatch.setattr(deps, "STATIONS_FILE", sf)
    monkeypatch.setattr(deps, "_stations_cache", None)
    raw = tmp_path / "1-raw"
    for j in (283, 284, 285):
        _raw_day(raw, 2026, j, ["AM.LIVE1"])
    monkeypatch.setattr(pl, "RAW_DIR", raw)

    r = TestClient(app).get("/api/station_health")
    assert r.status_code == 200
    by = {x["station"]: x for x in r.json()}
    assert by["LIVE1"]["status"] == "ok"
    assert by["OLD01"]["status"] == "retired"
    assert by["OLD01"]["end_date"] == "2026-02-23"
