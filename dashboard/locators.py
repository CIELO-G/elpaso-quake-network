"""Lazy initialisation of the two locators used by the review endpoint.

The grid-search locator (``lib.location.GridSearchLocator``) is the modern
relocator powering ``/api/event/{id}/relocate``. The GaMMA state dict is
held over from the legacy relocate path; it's no longer hit by the
relocate route but the routine that owns the sklearn shim still lives
here so any future caller goes through one place.

Both objects are expensive to construct (travel-time tables, sklearn
warm-up) so they're cached at module scope.
"""

from __future__ import annotations

from typing import Any

from dashboard.deps import load_stations
from lib.constants import (
    DEGREES_TO_KM,
    NETWORK_CENTER_LAT,
    NETWORK_CENTER_LON,
    NETWORK_HALF_WIDTH_DEG,
)

_gamma_state: dict | None = None
_grid_locator: Any = None  # lib.location.GridSearchLocator after init
_nlloc_locator: Any = None  # lib.location.nlloc.NLLocLocator after init


def init_nlloc_locator():
    """Build (or return the cached) NonLinLoc-backed relocator.

    Assumes ``scripts/nlloc_build_grids.py`` has been run once to produce
    the per-station travel-time grids; raises a clear ``FileNotFoundError``
    if not.
    """
    global _nlloc_locator
    if _nlloc_locator is not None:
        return _nlloc_locator

    from lib.location import Station
    from lib.location.nlloc import NLLocLocator

    raw = load_stations()
    stations = [
        Station(
            station_id=f"{s['network']}.{s['station']}",
            latitude=float(s["latitude"]),
            longitude=float(s["longitude"]),
            elevation_m=float(s.get("elevation_m", 0.0) or 0.0),
        )
        for s in raw
    ]
    _nlloc_locator = NLLocLocator(stations)
    return _nlloc_locator


def init_grid_search_locator():
    """Build (or return the cached) 1D layered grid-search locator.

    Precomputes per-station travel-time tables (~1 s for ~15 stations).
    """
    global _grid_locator
    if _grid_locator is not None:
        return _grid_locator

    from lib.location import (
        DEFAULT_WEST_TEXAS_MODEL,
        GridSearchLocator,
        Station,
    )

    raw = load_stations()
    stations = [
        Station(
            station_id=f"{s['network']}.{s['station']}",
            latitude=float(s["latitude"]),
            longitude=float(s["longitude"]),
            elevation_m=float(s.get("elevation_m", 0.0) or 0.0),
        )
        for s in raw
    ]
    _grid_locator = GridSearchLocator(
        stations,
        model=DEFAULT_WEST_TEXAS_MODEL,
        projection_center_latlon=(NETWORK_CENTER_LAT, NETWORK_CENTER_LON),
    )
    return _grid_locator


def init_gamma() -> dict:
    """Lazily build and cache the GaMMA station DataFrame + config.

    Returns a dict with keys ``stations_df``, ``proj``, ``config``,
    ``station_lookup``, ``center_lon``, ``center_lat``. Not currently used
    by the relocate endpoint (grid-search replaced GaMMA there), but kept
    available for any caller that still wants the GaMMA shape.
    """
    global _gamma_state
    if _gamma_state is not None:
        return _gamma_state

    # sklearn 1.4+ removed _check_n_features; GaMMA's vendored mixture
    # code still calls it. Same no-op shim as 4-association/associate.py.
    import sklearn.base

    if not hasattr(sklearn.base.BaseEstimator, "_check_n_features"):

        def _check_n_features(self, X, reset=False):
            n_features = X.shape[1] if hasattr(X, "shape") and X.ndim > 1 else 1
            if reset or not hasattr(self, "n_features_in_"):
                self.n_features_in_ = n_features

        sklearn.base.BaseEstimator._check_n_features = _check_n_features

    import numpy as np
    import pandas as pd

    from lib.projection import make_projection

    stations = load_stations()
    center_lon, center_lat = NETWORK_CENTER_LON, NETWORK_CENTER_LAT
    proj = make_projection(center_lon, center_lat)

    sta_rows = []
    sta_lookup = {}
    for s in stations:
        sid = f"{s['network']}.{s['station']}"
        x_km, y_km = proj(s["longitude"], s["latitude"])
        z_km = -s["elevation_m"] / 1000.0
        sta_rows.append({"id": sid, "x(km)": x_km, "y(km)": y_km, "z(km)": z_km})
        sta_lookup[sid] = {
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "elevation_m": s["elevation_m"],
        }
    stations_df = pd.DataFrame(sta_rows)

    xlim = ylim = NETWORK_HALF_WIDTH_DEG
    degree2km = DEGREES_TO_KM
    x_km_lim = np.array([-xlim, xlim]) * degree2km * np.cos(np.deg2rad(center_lat))
    y_km_lim = np.array([-ylim, ylim]) * degree2km
    dims = ["x(km)", "y(km)", "z(km)"]

    gamma_config = {
        "center": (center_lon, center_lat),
        "xlim_degree": [-xlim, xlim],
        "ylim_degree": [-ylim, ylim],
        "degree2km": degree2km,
        "dims": dims,
        "x(km)": x_km_lim.tolist(),
        "y(km)": y_km_lim.tolist(),
        "z(km)": [0, 30],
        "use_amplitude": False,
        "vel": {"p": 5.0, "s": 2.89},
        "use_dbscan": True,
        "dbscan_eps": 25,
        "dbscan_min_samples": 3,
        "min_picks_per_eq": 4,
        "max_sigma11": 2.0,
        "max_sigma22": 2.0,
        "oversample_factor": 5,
        "bfgs_bounds": ([x_km_lim.tolist(), y_km_lim.tolist(), [0, 30]] + [[None, None]]),
    }

    _gamma_state = {
        "stations_df": stations_df,
        "proj": proj,
        "config": gamma_config,
        "station_lookup": sta_lookup,
        "center_lon": center_lon,
        "center_lat": center_lat,
    }
    return _gamma_state
