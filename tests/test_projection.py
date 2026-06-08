"""Tests for lib/projection.py — coordinate projection utilities."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.constants import NETWORK_CENTER_LAT, NETWORK_CENTER_LON
from lib.projection import km_to_latlon, latlon_to_km, make_projection


class TestMakeProjection:
    """Tests for make_projection()."""

    def test_returns_callable(self):
        proj = make_projection()
        assert callable(proj)

    def test_center_maps_to_origin(self):
        proj = make_projection()
        x, y = proj(NETWORK_CENTER_LON, NETWORK_CENTER_LAT)
        assert x == pytest.approx(0.0, abs=0.001)
        assert y == pytest.approx(0.0, abs=0.001)

    def test_custom_center(self):
        proj = make_projection(center_lon=-100.0, center_lat=30.0)
        x, y = proj(-100.0, 30.0)
        assert x == pytest.approx(0.0, abs=0.001)
        assert y == pytest.approx(0.0, abs=0.001)


class TestLatlonToKm:
    """Tests for latlon_to_km()."""

    def test_center_returns_origin(self):
        x, y = latlon_to_km(NETWORK_CENTER_LAT, NETWORK_CENTER_LON)
        assert x == pytest.approx(0.0, abs=0.001)
        assert y == pytest.approx(0.0, abs=0.001)

    def test_one_degree_north_approx_111km(self):
        """One degree north of center should be ~111 km in y."""
        x, y = latlon_to_km(NETWORK_CENTER_LAT + 1.0, NETWORK_CENTER_LON)
        assert abs(x) < 1.0  # nearly zero E-W displacement
        assert y == pytest.approx(111.0, rel=0.02)  # ~111 km, 2% tolerance

    def test_one_degree_east_approx_96km(self):
        """One degree east at ~32N should be ~95-96 km in x."""
        x, y = latlon_to_km(NETWORK_CENTER_LAT, NETWORK_CENTER_LON + 1.0)
        assert x == pytest.approx(95.5, rel=0.03)  # ~95 km at this latitude
        assert abs(y) < 1.0

    def test_accepts_prebuilt_proj(self):
        proj = make_projection()
        x, y = latlon_to_km(NETWORK_CENTER_LAT, NETWORK_CENTER_LON, proj=proj)
        assert x == pytest.approx(0.0, abs=0.001)
        assert y == pytest.approx(0.0, abs=0.001)

    def test_station_r0f2d(self):
        """Real station R0F2D: 31.648649, -106.460311 — should be a few km from center."""
        x, y = latlon_to_km(31.648649, -106.460311)
        # South of center, slightly west
        assert y < 0  # south → negative y
        dist = (x**2 + y**2) ** 0.5
        assert 5 < dist < 30  # reasonable distance from center


class TestKmToLatlon:
    """Tests for km_to_latlon()."""

    def test_origin_returns_center(self):
        lon, lat = km_to_latlon(0.0, 0.0)
        assert lon == pytest.approx(NETWORK_CENTER_LON, abs=0.001)
        assert lat == pytest.approx(NETWORK_CENTER_LAT, abs=0.001)

    def test_accepts_prebuilt_proj(self):
        proj = make_projection()
        lon, lat = km_to_latlon(0.0, 0.0, proj=proj)
        assert lon == pytest.approx(NETWORK_CENTER_LON, abs=0.001)
        assert lat == pytest.approx(NETWORK_CENTER_LAT, abs=0.001)


class TestRoundTrip:
    """Verify latlon→km→latlon round-trip accuracy."""

    @pytest.mark.parametrize(
        "lat,lon",
        [
            (NETWORK_CENTER_LAT, NETWORK_CENTER_LON),  # center
            (31.648649, -106.460311),  # R0F2D
            (32.333333, -106.719858),  # R5912 (furthest north)
            (31.459459, -106.089922),  # RC9B8 (furthest south-east)
            (31.771774, -106.506378),  # KIDD
        ],
    )
    def test_round_trip_accuracy(self, lat, lon):
        """Round-trip should be accurate to <1 meter."""
        proj = make_projection()
        x, y = latlon_to_km(lat, lon, proj=proj)
        lon2, lat2 = km_to_latlon(x, y, proj=proj)
        assert lon2 == pytest.approx(lon, abs=1e-5)  # ~1 m accuracy
        assert lat2 == pytest.approx(lat, abs=1e-5)

    def test_round_trip_at_network_edge(self):
        """70 km offset from center should round-trip cleanly."""
        proj = make_projection()
        lon_out, lat_out = km_to_latlon(50.0, 50.0, proj=proj)
        x_back, y_back = latlon_to_km(lat_out, lon_out, proj=proj)
        assert x_back == pytest.approx(50.0, abs=0.01)
        assert y_back == pytest.approx(50.0, abs=0.01)
