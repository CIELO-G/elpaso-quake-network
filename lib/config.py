"""Shared configuration helpers for the El Paso seismic pipeline."""

import json

import yaml


def load_config(path, defaults=None):
    """Load YAML configuration and apply defaults."""
    with open(path) as f:
        config = yaml.safe_load(f)

    if defaults:
        for key, val in defaults.items():
            config.setdefault(key, val)

    return config


def load_stations(path):
    """Load and validate the station list from a JSON file."""
    with open(path) as f:
        stations = json.load(f)

    if not isinstance(stations, list) or len(stations) == 0:
        raise ValueError(f"stations file must contain a non-empty JSON array: {path}")

    for i, s in enumerate(stations):
        if "network" not in s or "station" not in s:
            raise ValueError(
                f"Station entry {i} missing required 'network' or 'station' field"
            )
    return stations
