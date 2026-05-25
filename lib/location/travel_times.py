"""Direct-ray travel-time computation through a 1D layered model.

Uses Snell's law along a flat-layered earth. Only the **direct** ray from
source to receiver is computed — head waves (refractions along a fast
layer) and diving rays are intentionally ignored. This is valid for:

* Local events at relatively short epicentral distance (up to ~50 km for
  typical induced-seismicity depths), where the direct arrival is first.
* Shallow sources (<~15 km) in crustal 1D models.

If the direct ray cannot reach the requested epicentral distance for a
given source depth (super-critical geometry), the returned travel time is
``NaN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from lib.location.velocity_model import LayeredModel, Phase

# Fractional margin below the critical slowness at which we truncate the
# takeoff-angle scan (avoids numerical blow-up near sin(theta)=1).
_P_MAX_SAFETY = 1.0 - 1e-6

# Number of slowness samples used when building the (distance, time) curve
# for a fixed source depth. Higher → more accurate interpolation.
_DEFAULT_N_SLOWNESS = 800


def _layer_boundaries_between(
    model: LayeredModel, z_lo: float, z_hi: float
) -> list[float]:
    """Return interior layer tops strictly between ``z_lo`` and ``z_hi``.

    Boundaries exactly at an endpoint are excluded. Result is sorted
    ascending.
    """
    if z_lo > z_hi:
        z_lo, z_hi = z_hi, z_lo
    return sorted(
        layer.top_km for layer in model.layers if z_lo < layer.top_km < z_hi
    )


def _integrate_ray(
    z_start: float,
    z_end: float,
    p: float,
    model: LayeredModel,
    phase: Phase,
) -> tuple[float, float]:
    """Integrate (horizontal distance, travel time) along a ray of slowness p.

    The ray travels between depths ``z_start`` and ``z_end`` (either
    direction — sign doesn't matter for cumulative distance/time). Segments
    are split at layer boundaries so each segment lies in a single layer.

    Returns ``(dx_km, dt_s)``. If any segment has ``p * v >= 1`` (super-
    critical), returns ``(inf, inf)``.
    """
    if z_start == z_end:
        return 0.0, 0.0

    z_lo = min(z_start, z_end)
    z_hi = max(z_start, z_end)
    waypoints = [z_lo] + _layer_boundaries_between(model, z_lo, z_hi) + [z_hi]

    dx_total = 0.0
    dt_total = 0.0
    for z1, z2 in zip(waypoints, waypoints[1:]):
        # Each segment lies within a single layer; sample velocity at the
        # midpoint to guarantee we're inside the layer (not on a boundary).
        mid = 0.5 * (z1 + z2)
        v = model.velocity(mid, phase)
        pv = p * v
        if pv >= 1.0:
            return float("inf"), float("inf")
        cos_theta = np.sqrt(1.0 - pv * pv)
        dz = z2 - z1  # always positive
        dx_total += dz * pv / cos_theta
        dt_total += dz / (v * cos_theta)
    return dx_total, dt_total


def _scan_slowness(
    source_depth_km: float,
    receiver_depth_km: float,
    model: LayeredModel,
    phase: Phase,
    n_samples: int = _DEFAULT_N_SLOWNESS,
) -> tuple[np.ndarray, np.ndarray]:
    """Scan slowness p from 0 up to near-critical; return (dx, dt) samples.

    Produces monotonically non-decreasing ``dx`` and ``dt`` arrays suitable
    for linear interpolation to find the travel time at a requested
    epicentral distance.
    """
    # For a direct ray to propagate through every layer, p * v < 1 must
    # hold in every layer along the path; the binding constraint is the
    # **fastest** layer along the path (p_max = 1 / v_max). For typical
    # crustal models where velocity increases with depth, this means the
    # deepest layer on the ray's path sets the bound.
    v_max = model.max_velocity(source_depth_km, receiver_depth_km, phase)
    p_max = _P_MAX_SAFETY / v_max

    # Nonlinear sampling: more density near p_max where dx grows fast.
    # A power-law transform concentrates samples near the upper end.
    u = np.linspace(0.0, 1.0, n_samples)
    p_arr = p_max * u ** 1.5

    dx = np.empty(n_samples)
    dt = np.empty(n_samples)
    for i, p in enumerate(p_arr):
        dx_i, dt_i = _integrate_ray(
            source_depth_km, receiver_depth_km, float(p), model, phase
        )
        dx[i] = dx_i
        dt[i] = dt_i

    # The scan should be monotonic; if numerical noise breaks strict
    # monotonicity, cumulative-maximum rectifies it for interpolation safety.
    dx = np.maximum.accumulate(dx)
    dt = np.maximum.accumulate(dt)
    return dx, dt


def travel_time(
    source_depth_km: float,
    receiver_depth_km: float,
    epicentral_distance_km: float,
    model: LayeredModel,
    phase: Phase,
    n_samples: int = _DEFAULT_N_SLOWNESS,
) -> float:
    """Direct-ray travel time from source to receiver, in seconds.

    Returns ``NaN`` if the direct ray cannot reach the requested epicentral
    distance (super-critical geometry — a head wave would be needed).
    """
    if epicentral_distance_km < 0:
        raise ValueError("epicentral distance must be non-negative")
    if epicentral_distance_km == 0.0:
        _, t = _integrate_ray(
            source_depth_km, receiver_depth_km, 0.0, model, phase
        )
        return t
    dx, dt = _scan_slowness(
        source_depth_km, receiver_depth_km, model, phase, n_samples
    )
    if epicentral_distance_km > dx[-1]:
        return float("nan")
    # Linear interpolation of dt as a function of dx.
    return float(np.interp(epicentral_distance_km, dx, dt))


# ---------------------------------------------------------------------------
# Precomputed travel-time table for fast lookup in the grid-search locator.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TravelTimeTable:
    """Precomputed direct-ray travel times on a (source_depth × distance) grid.

    Fields
    ------
    depths_km : np.ndarray, shape (N_z,)
        Ascending array of source depths.
    distances_km : np.ndarray, shape (N_d,)
        Ascending array of epicentral distances.
    tt : np.ndarray, shape (N_z, N_d)
        Travel times (seconds). ``NaN`` where the direct ray cannot reach.
    receiver_depth_km : float
        Receiver depth used when building the table (negative for an
        elevated station; zero at the datum).
    phase : {"P", "S"}
    """

    depths_km: np.ndarray
    distances_km: np.ndarray
    tt: np.ndarray
    receiver_depth_km: float
    phase: Phase

    def lookup(
        self, source_depths_km: np.ndarray, distances_km: np.ndarray
    ) -> np.ndarray:
        """Bilinear-interpolate travel times.

        Both inputs are broadcast to a common shape. Depths or distances
        outside the table extents are clamped to the nearest edge (depth)
        or return ``NaN`` (distance beyond max, indicating super-critical).
        """
        sd = np.asarray(source_depths_km, dtype=float)
        dd = np.asarray(distances_km, dtype=float)
        sd_b, dd_b = np.broadcast_arrays(sd, dd)
        shape = sd_b.shape
        sd_f = sd_b.ravel()
        dd_f = dd_b.ravel()

        # Depth: clamp to table extents before interpolation.
        z_min = self.depths_km[0]
        z_max = self.depths_km[-1]
        sd_c = np.clip(sd_f, z_min, z_max)

        # Find depth bracket indices.
        iz = np.searchsorted(self.depths_km, sd_c, side="right") - 1
        iz = np.clip(iz, 0, len(self.depths_km) - 2)
        z1 = self.depths_km[iz]
        z2 = self.depths_km[iz + 1]
        wz = np.where(z2 > z1, (sd_c - z1) / (z2 - z1), 0.0)

        # Distance: searchsorted on the distance axis.
        d_arr = self.distances_km
        idx = np.searchsorted(d_arr, dd_f, side="right") - 1
        idx = np.clip(idx, 0, len(d_arr) - 2)
        d1 = d_arr[idx]
        d2 = d_arr[idx + 1]
        wd = np.where(d2 > d1, (dd_f - d1) / (d2 - d1), 0.0)

        t11 = self.tt[iz, idx]
        t12 = self.tt[iz, idx + 1]
        t21 = self.tt[iz + 1, idx]
        t22 = self.tt[iz + 1, idx + 1]

        t = (
            (1 - wz) * ((1 - wd) * t11 + wd * t12)
            + wz * ((1 - wd) * t21 + wd * t22)
        )

        # Propagate NaN: if any corner is NaN, result is NaN. Clean up the
        # np.where result.
        bad = np.isnan(t11) | np.isnan(t12) | np.isnan(t21) | np.isnan(t22)
        t = np.where(bad, np.nan, t)

        # Distance beyond the table max: NaN (direct ray not reachable).
        t = np.where(dd_f > d_arr[-1], np.nan, t)

        return t.reshape(shape)


def build_tt_table(
    model: LayeredModel,
    source_depths_km: Sequence[float] | np.ndarray,
    distances_km: Sequence[float] | np.ndarray,
    receiver_depth_km: float,
    phase: Phase,
    n_slowness: int = _DEFAULT_N_SLOWNESS,
) -> TravelTimeTable:
    """Precompute a travel-time table over a grid of source depths and distances.

    For each source depth in ``source_depths_km``, a single slowness scan
    generates a (dx, dt) curve, which is then interpolated onto the
    requested ``distances_km``. Distances beyond the reachable maximum for
    that depth become ``NaN``.

    Returns a :class:`TravelTimeTable` for fast ``lookup`` in the locator.
    """
    sd = np.asarray(source_depths_km, dtype=float)
    dd = np.asarray(distances_km, dtype=float)
    if not np.all(np.diff(sd) > 0):
        raise ValueError("source_depths_km must be strictly ascending")
    if not np.all(np.diff(dd) > 0):
        raise ValueError("distances_km must be strictly ascending")

    tt = np.full((len(sd), len(dd)), np.nan, dtype=float)
    for i, z in enumerate(sd):
        dx, dt = _scan_slowness(
            float(z), receiver_depth_km, model, phase, n_slowness
        )
        # Interpolate only on the reachable portion; beyond dx[-1], leave NaN.
        reachable = dd <= dx[-1]
        if reachable.any():
            tt[i, reachable] = np.interp(dd[reachable], dx, dt)

    return TravelTimeTable(
        depths_km=sd,
        distances_km=dd,
        tt=tt,
        receiver_depth_km=receiver_depth_km,
        phase=phase,
    )
