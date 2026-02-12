"""Shared pytest fixtures for the El Paso seismic pipeline test suite."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a clean temporary directory."""
    return tmp_path


@pytest.fixture
def sample_stations_json(tmp_path) -> Path:
    """Write a minimal valid stations.json and return its path."""
    stations = [
        {
            "network": "AM",
            "station": "R0F2D",
            "location": "00",
            "channels": "EH?",
            "latitude": 31.648649,
            "longitude": -106.460311,
            "elevation_m": 1196.0,
            "sample_rate_hz": 100.0,
            "start_date": "2025-10-26",
            "model": "RS3D",
        },
        {
            "network": "AM",
            "station": "R4B41",
            "location": "00",
            "channels": "EH?",
            "latitude": 31.828829,
            "longitude": -106.531547,
            "elevation_m": 1258.0,
            "sample_rate_hz": 100.0,
            "start_date": "2025-05-01",
            "model": "RS3D",
        },
        {
            "network": "EP",
            "station": "KIDD",
            "location": "",
            "channels": "HH?",
            "fdsnws_url": "IRIS",
            "latitude": 31.771774,
            "longitude": -106.506378,
            "elevation_m": 1162.5,
            "sample_rate_hz": 100.0,
            "start_date": "2009-10-08",
            "model": "broadband",
        },
    ]
    path = tmp_path / "stations.json"
    path.write_text(json.dumps(stations, indent=2))
    return path


@pytest.fixture
def sample_config_yaml(tmp_path) -> Path:
    """Write a minimal config.yaml and return its path."""
    config = {
        "stations_file": "stations.json",
        "output_dir": str(tmp_path / "output"),
        "log_dir": str(tmp_path / "logs"),
        "log_max_bytes": 1048576,
        "log_backup_count": 1,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture
def sample_stations() -> list[dict]:
    """Return a list of sample station dicts."""
    return [
        {
            "network": "AM",
            "station": "R0F2D",
            "latitude": 31.648649,
            "longitude": -106.460311,
            "elevation_m": 1196.0,
            "location": "00",
            "channels": "EH?",
            "model": "RS3D",
        },
        {
            "network": "AM",
            "station": "R4B41",
            "latitude": 31.828829,
            "longitude": -106.531547,
            "elevation_m": 1258.0,
            "location": "00",
            "channels": "EH?",
            "model": "RS3D",
        },
    ]
