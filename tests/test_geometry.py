"""B5: one study-area definition, consumed everywhere."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lib import constants
from lib.geometry import grid_half_extent_km, inside_study_area, study_area
from lib.location import nlloc_config
from lib.models import StationValidationError, validate_stations

ROOT = Path(__file__).resolve().parents[1]


def test_half_width_is_one_degree():
    assert constants.NETWORK_HALF_WIDTH_DEG == 1.0


def test_grid_extent_matches_built_grids():
    """The extents derived from constants must equal what the NLLoc grids on disk use."""
    assert grid_half_extent_km() == (94.0, 111.0)
    assert (nlloc_config.GRID_X_HALF_KM, nlloc_config.GRID_Y_HALF_KM) == (94.0, 111.0)
    hdr = next((ROOT / "output/nlloc/grids/time").glob("*.P.*.time.hdr"), None)
    if hdr is None:
        pytest.skip("NLLoc grids not built")
    nx, ny, nz, x0, y0, z0 = hdr.read_text().split()[:6]
    assert (float(x0), float(y0)) == (-94.0, -111.0)


def test_study_area_dict():
    a = study_area()
    assert a["lat_min"] == pytest.approx(30.85) and a["lon_max"] == pytest.approx(-105.40)
    assert a["x_half_km"] == 94.0 and a["y_half_km"] == 111.0


def test_inside_study_area():
    assert inside_study_area(31.85, -106.40)
    assert inside_study_area(32.84, -105.41)
    assert not inside_study_area(32.86, -106.40)
    assert not inside_study_area(31.85, -107.41)


def test_station_outside_box_rejected():
    entry = {"network": "AM", "station": "FAR01", "latitude": 33.5, "longitude": -106.4,
             "elevation_m": 1200.0, "start_date": "2025-01-01", "channels": "EH?",
             "instrument": {"geophone": {"sensitivity": 3.9965e8}}}
    with pytest.raises(StationValidationError, match="FAR01.*outside the study area"):
        validate_stations([entry])


def test_association_config_has_no_geometry_keys():
    text = (ROOT / "4-association/config.yaml").read_text()
    for key in ("center_lat", "center_lon", "xlim_degree", "ylim_degree"):
        assert not re.search(rf"^{key}\s*:", text, re.M), f"{key} must not be set in config.yaml"


def test_study_area_endpoint():
    from dashboard.app import app
    r = TestClient(app).get("/api/study_area")
    assert r.status_code == 200
    assert r.json()["half_width_deg"] == 1.0
