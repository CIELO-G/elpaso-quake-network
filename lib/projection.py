"""Coordinate projection utilities for the El Paso seismic pipeline.

Wraps pyproj stereographic projection centred on the network for converting
station lat/lon to local x/y (km) coordinates used by GaMMA.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyproj import Proj

from lib.constants import NETWORK_CENTER_LAT, NETWORK_CENTER_LON


def make_projection(
    center_lon: float = NETWORK_CENTER_LON,
    center_lat: float = NETWORK_CENTER_LAT,
) -> "Proj":
    """Create a stereographic Proj centred on the network.

    Parameters
    ----------
    center_lon, center_lat : float
        Projection centre in decimal degrees.

    Returns
    -------
    pyproj.Proj
        Projection object. Call ``proj(lon, lat)`` to get ``(x_km, y_km)``
        and ``proj(x, y, inverse=True)`` for the reverse.
    """
    from pyproj import Proj

    return Proj(f"+proj=stere +lon_0={center_lon} +lat_0={center_lat} +units=km")


def latlon_to_km(
    lat: float,
    lon: float,
    proj: "Proj | None" = None,
    center_lon: float = NETWORK_CENTER_LON,
    center_lat: float = NETWORK_CENTER_LAT,
) -> tuple[float, float]:
    """Convert latitude/longitude to x/y in km using stereographic projection.

    Parameters
    ----------
    lat, lon : float
        Geographic coordinates.
    proj : Proj, optional
        Pre-built projection. If None, one is created from centre coords.
    center_lon, center_lat : float
        Used only when *proj* is None.

    Returns
    -------
    (x_km, y_km) : tuple[float, float]
    """
    if proj is None:
        proj = make_projection(center_lon, center_lat)
    return proj(lon, lat)


def km_to_latlon(
    x_km: float,
    y_km: float,
    proj: "Proj | None" = None,
    center_lon: float = NETWORK_CENTER_LON,
    center_lat: float = NETWORK_CENTER_LAT,
) -> tuple[float, float]:
    """Convert x/y km back to latitude/longitude.

    Returns
    -------
    (lon, lat) : tuple[float, float]
    """
    if proj is None:
        proj = make_projection(center_lon, center_lat)
    return proj(x_km, y_km, inverse=True)
