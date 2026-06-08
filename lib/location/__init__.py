"""Seismic event location utilities for the El Paso pipeline.

Provides a probabilistic grid-search locator that uses direct P and S rays
through a 1D layered velocity model. Designed for local/regional events
(induced seismicity, distances <~100 km, shallow sources).
"""

from lib.location.grid_search import (
    GridSearchLocator,
    LocationResult,
    Pick,
    Residual,
    Station,
)
from lib.location.travel_times import (
    TravelTimeTable,
    build_tt_table,
    travel_time,
)
from lib.location.velocity_model import (
    DEFAULT_WEST_TEXAS_MODEL,
    Layer,
    LayeredModel,
)

__all__ = [
    "DEFAULT_WEST_TEXAS_MODEL",
    "GridSearchLocator",
    "Layer",
    "LayeredModel",
    "LocationResult",
    "Pick",
    "Residual",
    "Station",
    "TravelTimeTable",
    "build_tt_table",
    "travel_time",
]
