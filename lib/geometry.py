"""The study area, derived once from lib.constants (review item B5).

Before this, the box was written in four places with two values
(association config ±1.0°, constants 0.7°, NLLoc grid ±94/±111 km, a
hard-coded map rectangle). A station outside the box breaks Grid2Time and
is invisible on the map, so every consumer now reads it from here.
"""

from __future__ import annotations

import math
from typing import Any

from lib.constants import (
    DEGREES_TO_KM,
    NETWORK_CENTER_LAT,
    NETWORK_CENTER_LON,
    NETWORK_HALF_WIDTH_DEG,
)


def grid_half_extent_km(
    half_width_deg: float = NETWORK_HALF_WIDTH_DEG,
    center_lat: float = NETWORK_CENTER_LAT,
) -> tuple[float, float]:
    """(x_half_km, y_half_km) of the projected box, rounded to whole km.

    Rounding keeps the NLLoc grid origin on a whole-km node and reproduces
    the extents the existing grids were built with (94, 111).
    """
    x = round(half_width_deg * DEGREES_TO_KM * math.cos(math.radians(center_lat)))
    y = round(half_width_deg * DEGREES_TO_KM)
    return float(x), float(y)


def study_area() -> dict[str, Any]:
    """Degrees and km description of the box, as the dashboard serves it."""
    x_km, y_km = grid_half_extent_km()
    return {
        "center_lat": NETWORK_CENTER_LAT,
        "center_lon": NETWORK_CENTER_LON,
        "half_width_deg": NETWORK_HALF_WIDTH_DEG,
        "lat_min": NETWORK_CENTER_LAT - NETWORK_HALF_WIDTH_DEG,
        "lat_max": NETWORK_CENTER_LAT + NETWORK_HALF_WIDTH_DEG,
        "lon_min": NETWORK_CENTER_LON - NETWORK_HALF_WIDTH_DEG,
        "lon_max": NETWORK_CENTER_LON + NETWORK_HALF_WIDTH_DEG,
        "x_half_km": x_km,
        "y_half_km": y_km,
    }


def inside_study_area(latitude: float, longitude: float, margin_deg: float = 0.0) -> bool:
    """True when the point lies within the box shrunk by ``margin_deg``."""
    h = NETWORK_HALF_WIDTH_DEG - margin_deg
    return (
        abs(latitude - NETWORK_CENTER_LAT) <= h
        and abs(longitude - NETWORK_CENTER_LON) <= h
    )
