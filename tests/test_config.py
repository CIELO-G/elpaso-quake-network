"""Tests for lib/config.py — configuration loading and validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations


class TestLoadConfig:
    """Tests for load_config()."""

    def test_load_simple_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"key": "value", "num": 42}))
        result = load_config(config_path)
        assert result["key"] == "value"
        assert result["num"] == 42

    def test_defaults_applied(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"key": "value"}))
        defaults = {"missing_key": "default_val", "key": "should_not_override"}
        result = load_config(config_path, defaults=defaults)
        assert result["key"] == "value"  # existing key not overridden
        assert result["missing_key"] == "default_val"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_empty_yaml(self, tmp_path):
        config_path = tmp_path / "empty.yaml"
        config_path.write_text("")
        result = load_config(config_path, defaults={"a": 1})
        assert result["a"] == 1


class TestLoadStations:
    """Tests for load_stations()."""

    def test_load_valid_stations(self, sample_stations_json):
        stations = load_stations(sample_stations_json)
        assert len(stations) == 3
        assert stations[0]["network"] == "AM"
        assert stations[0]["station"] == "R0F2D"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_stations(tmp_path / "nonexistent.json")

    def test_empty_array_raises(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("[]")
        with pytest.raises(ValueError, match="non-empty"):
            load_stations(path)

    def test_missing_required_fields_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps([{"network": "AM"}]))
        with pytest.raises(ValueError, match="missing required"):
            load_stations(path)

    def test_invalid_latitude_raises(self, tmp_path):
        path = tmp_path / "bad_lat.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "network": "AM",
                        "station": "TEST",
                        "latitude": 100.0,
                        "longitude": -106.0,
                        "elevation_m": 1000.0,
                    }
                ]
            )
        )
        with pytest.raises(ValueError, match="latitude"):
            load_stations(path)

    def test_invalid_longitude_raises(self, tmp_path):
        path = tmp_path / "bad_lon.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "network": "AM",
                        "station": "TEST",
                        "latitude": 31.0,
                        "longitude": -200.0,
                        "elevation_m": 1000.0,
                    }
                ]
            )
        )
        with pytest.raises(ValueError, match="longitude"):
            load_stations(path)
