"""Local magnitude (ML) computation for the El Paso seismic pipeline.

Implements Hutton & Boore (1987) ML formula, extracted from associate.py
for independent testability.

Formula
-------
    ML_sta = log10(A_wa_mm) + a * log10(r / r_ref) + b * (r - r_ref) + c

Where A_wa_mm is the synthetic Wood-Anderson displacement amplitude in mm,
computed from measured velocity amplitude.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class MLConfig:
    """Configuration for local magnitude computation."""

    freq_hz: float = 5.0
    wa_gain: float = 2800.0
    min_distance_km: float = 10.0
    a: float = 1.110
    b: float = 0.00189
    c: float = 3.0
    ref_distance_km: float = 100.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in km (Haversine formula)."""
    R = 6371.0
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def velocity_to_wa_displacement_mm(
    amp_vel_m_per_s: float,
    freq_hz: float = 5.0,
    wa_gain: float = 2080.0,
) -> float:
    """Convert peak velocity (m/s) to synthetic Wood-Anderson displacement (mm).

    Parameters
    ----------
    amp_vel_m_per_s : float
        Peak absolute velocity amplitude in m/s.
    freq_hz : float
        Dominant frequency for vel-to-displacement conversion.
    wa_gain : float
        Wood-Anderson static magnification.

    Returns
    -------
    float
        Wood-Anderson displacement amplitude in mm.
    """
    # vel (m/s) -> disp (m): divide by 2*pi*f
    # disp (m) -> WA disp (m): multiply by gain
    # WA disp (m) -> WA disp (mm): multiply by 1000
    return amp_vel_m_per_s * wa_gain * 1000.0 / (2.0 * math.pi * freq_hz)


def compute_ml_station(
    amp_vel_m_per_s: float,
    hypocentral_distance_km: float,
    cfg: MLConfig | None = None,
) -> float | None:
    """Compute single-station ML from velocity amplitude and distance.

    Parameters
    ----------
    amp_vel_m_per_s : float
        Peak absolute velocity amplitude in m/s.
    hypocentral_distance_km : float
        3D distance from event hypocenter to station in km.
    cfg : MLConfig, optional
        ML parameters. Uses defaults if None.

    Returns
    -------
    float or None
        Station ML value, or None if inputs are invalid.
    """
    if cfg is None:
        cfg = MLConfig()

    if amp_vel_m_per_s <= 0 or hypocentral_distance_km < cfg.min_distance_km:
        return None

    a_wa_mm = velocity_to_wa_displacement_mm(amp_vel_m_per_s, cfg.freq_hz, cfg.wa_gain)
    if a_wa_mm <= 0:
        return None

    r = hypocentral_distance_km
    r_ref = cfg.ref_distance_km

    return math.log10(a_wa_mm) + cfg.a * math.log10(r / r_ref) + cfg.b * (r - r_ref) + cfg.c


def compute_ml_network(
    station_mls: list[float],
) -> tuple[float | None, float | None, int]:
    """Compute network ML from individual station readings.

    Uses median (robust to outliers) and standard deviation
    per standard seismological practice (SCIENCE_AUDIT.md Issue 17).

    Parameters
    ----------
    station_mls : list[float]
        List of per-station ML values.

    Returns
    -------
    (ml, ml_err, count) : tuple
        Median ML, standard deviation (None if < 2 readings), and count.
        Returns (None, None, 0) if no readings.
    """
    if not station_mls:
        return None, None, 0

    n = len(station_mls)
    ml = statistics.median(station_mls)
    ml_err = statistics.stdev(station_mls) if n >= 2 else None
    return ml, ml_err, n
