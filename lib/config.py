"""Shared configuration helpers for the El Paso seismic pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from lib.models import validate_stations


def load_config(path: str | Path, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load YAML configuration and apply defaults.

    Parameters
    ----------
    path : str or Path
        Path to the YAML config file.
    defaults : dict, optional
        Default values applied for missing keys.

    Returns
    -------
    dict
        Merged configuration dictionary.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    yaml.YAMLError
        If the file is not valid YAML.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path) as f:
        config = yaml.safe_load(f)

    if config is None:
        config = {}

    if defaults:
        for key, val in defaults.items():
            config.setdefault(key, val)

    return config


def load_stations(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate the station list from a JSON file.

    Parameters
    ----------
    path : str or Path
        Path to stations.json.

    Returns
    -------
    list[dict]
        Validated list of station entries.

    Raises
    ------
    FileNotFoundError
        If the stations file does not exist.
    ValueError
        If the file content is invalid.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Stations file not found: {path}")

    with open(path) as f:
        stations = json.load(f)

    # Run validation (raises StationValidationError for critical issues)
    warnings = validate_stations(stations)
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)

    return stations


def validate_config_cli(config_path: str, stations_path: str | None = None) -> bool:
    """Validate a configuration file and optionally a stations file.

    Returns True if all validations pass, False otherwise.
    Prints issues to stderr.
    """
    ok = True

    try:
        config = load_config(config_path)
        print(f"Config OK: {config_path} ({len(config)} keys)")
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"Config FAIL: {config_path}: {e}", file=sys.stderr)
        ok = False

    if stations_path:
        try:
            stations = load_stations(stations_path)
            print(f"Stations OK: {stations_path} ({len(stations)} stations)")
        except (FileNotFoundError, ValueError) as e:
            print(f"Stations FAIL: {stations_path}: {e}", file=sys.stderr)
            ok = False

    return ok
