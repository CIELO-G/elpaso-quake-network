"""scripts/add_station.py: validated add/retire of stations.json entries (B9)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(tmp_file):
    spec = importlib.util.spec_from_file_location("add_station_mod", ROOT / "scripts/add_station.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["add_station_mod"] = mod
    spec.loader.exec_module(mod)
    mod.STATIONS_FILE = tmp_file
    return mod


def _args(**kw):
    import argparse
    base = dict(network="AM", location="00", channels=None, sample_rate=100.0, fdsnws_url=None,
                skip_fdsn_check=True, skip_grids=True, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def stations_file(tmp_path):
    f = tmp_path / "stations.json"
    f.write_text((ROOT / "stations.json").read_text())
    return f


def test_add_writes_validated_entry(stations_file):
    m = _load(stations_file)
    m.cmd_add(_args(station="R9999", lat=31.90, lon=-106.45, elev=1200.0, model="RS3D",
                    start_date="2026-11-01"))
    data = json.loads(stations_file.read_text())
    new = data[-1]
    assert new["station"] == "R9999" and new["channels"] == "EH?"
    assert new["instrument"]["geophone"]["sensitivity"] > 0
    from lib.models import validate_stations
    validate_stations(data)


def test_add_rejects_outside_study_area_and_duplicates(stations_file):
    m = _load(stations_file)
    with pytest.raises(SystemExit, match="outside the study area"):
        m.cmd_add(_args(station="R9998", lat=33.5, lon=-106.45, elev=1200.0, model="RS3D",
                        start_date="2026-11-01"))
    with pytest.raises(SystemExit, match="already in stations.json"):
        m.cmd_add(_args(station="RDB14", lat=31.9, lon=-106.45, elev=1200.0, model="RS3D",
                        start_date="2026-11-01"))
    assert len(json.loads(stations_file.read_text())) == 14  # untouched


def test_rs4d_and_dry_run(stations_file):
    m = _load(stations_file)
    m.cmd_add(_args(station="R9997", lat=31.9, lon=-106.45, elev=1200.0, model="RS4D",
                    start_date="2026-11-01", dry_run=True))
    assert len(json.loads(stations_file.read_text())) == 14
    m.cmd_add(_args(station="R9997", lat=31.9, lon=-106.45, elev=1200.0, model="RS4D",
                    start_date="2026-11-01"))
    new = json.loads(stations_file.read_text())[-1]
    assert new["channels"] == "E??" and "accelerometer" in new["instrument"]


def test_retire_sets_end_date(stations_file):
    import argparse
    m = _load(stations_file)
    m.cmd_retire(argparse.Namespace(station="R9070", network=None, end_date="2026-12-31", dry_run=False))
    data = json.loads(stations_file.read_text())
    assert next(s for s in data if s["station"] == "R9070")["end_date"] == "2026-12-31"
    with pytest.raises(SystemExit, match="not in stations.json"):
        m.cmd_retire(argparse.Namespace(station="NOPE1", network=None, end_date="2026-12-31", dry_run=False))
    with pytest.raises(SystemExit, match="before start_date"):
        m.cmd_retire(argparse.Namespace(station="R9070", network=None, end_date="2020-01-01", dry_run=False))
