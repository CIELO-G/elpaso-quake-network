"""Tests for lib/models.py — data models and validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.models import (
    Event,
    Pick,
    Station,
    StationValidationError,
    validate_station,
    validate_stations,
)


class TestStation:
    """Tests for Station dataclass."""

    def test_from_dict(self):
        d = {
            "network": "AM",
            "station": "R0F2D",
            "latitude": 31.65,
            "longitude": -106.46,
            "elevation_m": 1196.0,
        }
        s = Station.from_dict(d)
        assert s.network == "AM"
        assert s.station == "R0F2D"
        assert s.id == "AM.R0F2D"
        assert s.location == "00"  # default

    def test_from_dict_with_optional_fields(self):
        d = {
            "network": "EP",
            "station": "KIDD",
            "latitude": 31.77,
            "longitude": -106.51,
            "elevation_m": 1162.5,
            "location": "",
            "channels": "HH?",
            "model": "broadband",
            "fdsnws_url": "IRIS",
        }
        s = Station.from_dict(d)
        assert s.location == ""
        assert s.channels == "HH?"
        assert s.fdsnws_url == "IRIS"


class TestPick:
    """Tests for Pick dataclass."""

    def test_basic(self):
        p = Pick(
            network="AM",
            station="R0F2D",
            location="00",
            channel="EHZ",
            phase="P",
            time="2026-01-15T12:00:00.123",
            probability=0.85,
        )
        assert p.phase == "P"
        assert p.amplitude is None


class TestEvent:
    """Tests for Event dataclass."""

    def test_basic(self):
        e = Event(
            event_id="ep20260115-0001",
            time="2026-01-15T12:00:00",
            latitude=31.85,
            longitude=-106.40,
            depth_km=5.0,
        )
        assert e.magnitude is None
        assert e.num_picks == 0


class TestValidateStation:
    """Tests for validate_station()."""

    def test_valid_station(self):
        entry = {
            "network": "AM",
            "station": "R0F2D",
            "latitude": 31.65,
            "longitude": -106.46,
            "elevation_m": 1196.0,
            "channels": "EH?",
        }
        warnings = validate_station(entry, 0)
        assert warnings == []

    def test_missing_required_fields(self):
        with pytest.raises(StationValidationError, match="missing required"):
            validate_station({"network": "AM"}, 0)

    def test_latitude_out_of_range(self):
        entry = {
            "network": "AM",
            "station": "TEST",
            "latitude": 95.0,
            "longitude": -106.0,
            "elevation_m": 1000.0,
        }
        with pytest.raises(StationValidationError, match="latitude"):
            validate_station(entry, 0)

    def test_longitude_out_of_range(self):
        entry = {
            "network": "AM",
            "station": "TEST",
            "latitude": 31.0,
            "longitude": -200.0,
            "elevation_m": 1000.0,
        }
        with pytest.raises(StationValidationError, match="longitude"):
            validate_station(entry, 0)

    def test_unusual_elevation_warning(self):
        entry = {
            "network": "AM",
            "station": "TEST",
            "latitude": 31.0,
            "longitude": -106.0,
            "elevation_m": -600.0,
        }
        warnings = validate_station(entry, 0)
        assert any("elevation" in w for w in warnings)

    def test_unusual_channel_warning(self):
        entry = {
            "network": "AM",
            "station": "TEST",
            "latitude": 31.0,
            "longitude": -106.0,
            "elevation_m": 1000.0,
            "channels": "XYZ",
        }
        warnings = validate_station(entry, 0)
        assert any("channel" in w for w in warnings)


class TestValidateStations:
    """Tests for validate_stations()."""

    def test_empty_list_raises(self):
        with pytest.raises(StationValidationError, match="non-empty"):
            validate_stations([])

    def test_non_list_raises(self):
        with pytest.raises(StationValidationError, match="non-empty"):
            validate_stations("not a list")

    def test_valid_stations(self):
        stations = [
            {
                "network": "AM",
                "station": "R0F2D",
                "latitude": 31.65,
                "longitude": -106.46,
                "elevation_m": 1196.0,
            },
        ]
        warnings = validate_stations(stations)
        assert isinstance(warnings, list)
