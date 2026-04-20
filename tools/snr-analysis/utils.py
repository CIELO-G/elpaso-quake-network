"""Shared helpers for SNR analysis."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import yaml
from obspy import UTCDateTime
from obspy.geodetics import gps2dist_azimuth


def load_config(config_path: str = "config.yaml") -> dict:
    """Load YAML config and resolve paths relative to the config file."""
    cfg_path = Path(config_path).resolve()
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    # Resolve relative paths from config file directory
    base = cfg_path.parent
    for key in ("pipeline_root", "stations_file", "catalog_file",
                "assignments_file", "processed_dir", "output_dir"):
        if key in cfg and cfg[key]:
            cfg[key] = str((base / cfg[key]).resolve())
    return cfg


def load_stations(stations_file: str) -> list[dict]:
    """Load stations.json."""
    with open(stations_file) as f:
        return json.load(f)


def station_distance_azimuth(
    station: dict,
    ev_lat: float,
    ev_lon: float,
) -> tuple[float, float]:
    """Return (distance_km, azimuth_deg) from event to station."""
    dist_m, az, _ = gps2dist_azimuth(ev_lat, ev_lon,
                                      station["latitude"], station["longitude"])
    return dist_m / 1000.0, az


def get_site_type(station_name: str, site_types: dict) -> str:
    """Look up site type for a station."""
    for stype, stations in site_types.items():
        if station_name in stations:
            return stype
    return "unknown"


def compute_rms(data: np.ndarray) -> float:
    """RMS of a numpy array."""
    if len(data) == 0:
        return 0.0
    return float(np.sqrt(np.mean(data ** 2)))


def snr_ratio(signal_rms: float, noise_rms: float) -> float:
    """SNR as a linear ratio. Returns 0 if noise is zero."""
    if noise_rms == 0:
        return 0.0
    return signal_rms / noise_rms


def snr_db(snr_linear: float) -> float:
    """Convert linear SNR to dB."""
    if snr_linear <= 0:
        return -np.inf
    return 20.0 * math.log10(snr_linear)
