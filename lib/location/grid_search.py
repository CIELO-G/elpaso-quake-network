"""Probabilistic grid-search locator for local seismic events.

Algorithm
---------
For a candidate source at ``(x, y, z, t_0)`` and stations at known
positions, direct-ray travel times ``tt_i`` are precomputed via a 1D
layered velocity model. The weighted chi-squared misfit is

    chi2(x, y, z, t_0) = sum_i ((obs_i - t_0 - tt_i(x, y, z)) / sigma_i)^2

For each spatial grid node ``(x, y, z)``, the optimal origin time is the
weighted-LS solution

    t_0*(x, y, z) = sum_i w_i (obs_i - tt_i) / sum_i w_i
                    with w_i = 1 / sigma_i^2

so origin time is not searched over the grid — only ``(x, y, z)``.

The best location is the ``argmin`` of ``chi2`` over the grid. The
posterior density ``P(x, y, z) ~ exp(-0.5 * (chi2 - chi2_min))`` is
normalised over the grid and used to compute a 3x3 covariance matrix and
confidence ellipsoids for reporting.

Assumptions / known limits
--------------------------
* Direct rays only — no head waves or diving rays. Events whose first
  arrival at a station should be a head wave will have that station's
  contribution flagged as unreachable (``NaN`` travel time), which the
  locator handles by dropping that pick from the chi2 sum at that grid
  node.
* Flat-earth geometry. Accurate to ~1% at sub-200-km scales.
* Fixed per-phase pick uncertainty. Callers may override via the
  ``Pick.sigma_s`` field on a per-pick basis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

from lib.location.travel_times import (
    TravelTimeTable,
    build_tt_table,
)
from lib.location.velocity_model import (
    DEFAULT_WEST_TEXAS_MODEL,
    LayeredModel,
    Phase,
)
from lib.projection import km_to_latlon, latlon_to_km, make_projection

# Default per-phase pick uncertainties (seconds). Roughly matches what is
# used in many published local-earthquake location studies.
DEFAULT_SIGMA_P = 0.10
DEFAULT_SIGMA_S = 0.20

# chi^2 increments for confidence regions on a 3D Gaussian posterior:
#   chi2.ppf(0.68, df=3) = 3.5059
#   chi2.ppf(0.95, df=3) = 7.8147
_CHI2_68_3D = 3.506
_CHI2_95_3D = 7.815


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Station:
    """A seismic station used by the locator.

    Parameters
    ----------
    station_id : str
        Dotted ``NETWORK.STATION`` identifier (e.g. ``"AM.R3B56"``).
    latitude, longitude : float
        Geographic coordinates, decimal degrees.
    elevation_m : float
        Elevation above the datum (sea level), metres. Translated to
        ``receiver_depth_km = -elevation_m / 1000`` in the model.
    """

    station_id: str
    latitude: float
    longitude: float
    elevation_m: float = 0.0


@dataclass
class Pick:
    """A P- or S-phase pick at one station.

    ``time_s`` is seconds relative to an arbitrary reference epoch chosen
    by the locator (for numerical stability). Callers can instead pass
    :class:`obspy.UTCDateTime` objects via the ``time`` argument of
    :meth:`GridSearchLocator.locate` and the locator will convert.
    """

    station_id: str
    phase: Phase
    time_s: float
    sigma_s: float | None = None  # None → use per-phase default

    def __post_init__(self) -> None:
        if self.phase not in ("P", "S"):
            raise ValueError(f"phase must be 'P' or 'S', got {self.phase!r}")
        if self.sigma_s is not None and self.sigma_s <= 0:
            raise ValueError(f"sigma_s must be positive, got {self.sigma_s}")


@dataclass(frozen=True)
class Residual:
    """Per-pick residual at the best-fit hypocentre."""

    station_id: str
    phase: Phase
    observed_s: float
    predicted_s: float
    residual_s: float
    weight: float


@dataclass(frozen=True)
class LocationResult:
    """Output of :meth:`GridSearchLocator.locate`."""

    latitude: float
    longitude: float
    depth_km: float
    origin_time_s: float
    """Origin time in the same reference frame as input pick times."""

    n_picks_used: int
    rms_residual_s: float

    chi2_min: float
    """Minimum chi-squared over the grid."""

    # 3x3 covariance in local cartesian (x_km, y_km, z_km), computed from
    # the normalised posterior over the grid.
    covariance_xyz_km2: np.ndarray
    """3x3 covariance matrix in local cartesian km (x, y, depth)."""

    horizontal_semi_axis_major_km: float
    horizontal_semi_axis_minor_km: float
    horizontal_semi_axis_azimuth_deg: float
    """95% confidence horizontal error ellipse, in local cartesian frame."""

    sigma_depth_km: float
    """Marginal 1-sigma depth uncertainty, km."""

    residuals: tuple[Residual, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Locator
# ---------------------------------------------------------------------------


class GridSearchLocator:
    """Probabilistic grid-search locator using a 1D layered velocity model.

    Construction precomputes a travel-time table per station per phase
    (fast: <1 s for a typical network). The :meth:`locate` method runs one
    event; the locator can be reused across events.

    Parameters
    ----------
    stations : sequence of Station
        All stations that may contribute picks. Unknown stations in a
        pick set will be silently ignored at :meth:`locate` time.
    model : LayeredModel
        1D velocity model. Defaults to ``DEFAULT_WEST_TEXAS_MODEL``.
    tt_depth_min_km, tt_depth_max_km : float
        Depth extent of the internal travel-time tables. Grid-search
        depths outside this range will be clamped.
    tt_depth_step_km : float
        Depth sampling of the internal tables. Finer → more accurate
        lookups at the cost of memory/build time.
    tt_distance_max_km : float
        Maximum epicentral distance in the internal tables. Stations
        farther than this from a grid node contribute NaN and are
        dropped.
    tt_distance_step_km : float
        Distance sampling of the internal tables.
    """

    def __init__(
        self,
        stations: Sequence[Station],
        model: LayeredModel = DEFAULT_WEST_TEXAS_MODEL,
        *,
        tt_depth_min_km: float = 0.0,
        tt_depth_max_km: float = 25.0,
        tt_depth_step_km: float = 0.25,
        tt_distance_max_km: float = 250.0,
        tt_distance_step_km: float = 0.5,
        projection_center_latlon: tuple[float, float] | None = None,
    ) -> None:
        if not stations:
            raise ValueError("at least one station required")
        self.model = model
        self.stations: dict[str, Station] = {s.station_id: s for s in stations}

        # Projection for lat/lon <-> local km. Default: centre at the
        # station centroid; caller can override for deterministic frames.
        if projection_center_latlon is None:
            clat = np.mean([s.latitude for s in stations])
            clon = np.mean([s.longitude for s in stations])
        else:
            clat, clon = projection_center_latlon
        self._proj = make_projection(center_lon=clon, center_lat=clat)
        self._proj_center = (float(clat), float(clon))

        # Cache station positions in local km.
        self._station_xy: dict[str, tuple[float, float]] = {}
        for s in stations:
            x, y = latlon_to_km(s.latitude, s.longitude, proj=self._proj)
            self._station_xy[s.station_id] = (float(x), float(y))

        # Shared depth and distance axes for all tables.
        depths = np.arange(
            tt_depth_min_km,
            tt_depth_max_km + 0.5 * tt_depth_step_km,
            tt_depth_step_km,
        )
        distances = np.arange(
            0.0,
            tt_distance_max_km + 0.5 * tt_distance_step_km,
            tt_distance_step_km,
        )

        # Precompute (station, phase) → TravelTimeTable.
        self._tt: dict[tuple[str, Phase], TravelTimeTable] = {}
        for s in stations:
            rx_z = -float(s.elevation_m) / 1000.0
            for phase in ("P", "S"):
                self._tt[(s.station_id, phase)] = build_tt_table(
                    model=model,
                    source_depths_km=depths,
                    distances_km=distances,
                    receiver_depth_km=rx_z,
                    phase=phase,  # type: ignore[arg-type]
                )

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    def locate(
        self,
        picks: Iterable[Pick],
        *,
        x_half_width_km: float = 40.0,
        y_half_width_km: float = 40.0,
        depth_min_km: float = 0.0,
        depth_max_km: float = 20.0,
        horizontal_spacing_km: float = 0.25,
        depth_spacing_km: float = 0.25,
        center_xy_km: tuple[float, float] | None = None,
        sigma_p: float = DEFAULT_SIGMA_P,
        sigma_s: float = DEFAULT_SIGMA_S,
        min_picks: int = 4,
    ) -> LocationResult:
        """Locate a single event from a set of picks.

        Parameters
        ----------
        picks
            Iterable of :class:`Pick`. Picks with ``station_id`` not in
            the locator's station catalog are silently skipped.
        x_half_width_km, y_half_width_km
            Search extent in each horizontal direction around
            ``center_xy_km`` (local cartesian). Default 40 km half-width.
        depth_min_km, depth_max_km
            Depth search extent.
        horizontal_spacing_km, depth_spacing_km
            Grid resolution.
        center_xy_km
            Centre of the search volume in local cartesian km. If None,
            uses the station-weighted centroid of contributing picks.
        sigma_p, sigma_s
            Default per-phase pick uncertainty (s). Overridden by
            ``Pick.sigma_s`` when set on a given pick.
        min_picks
            Minimum number of usable picks. Need at least 4 total (or 3
            with depth fixed — not currently supported).

        Returns
        -------
        LocationResult
        """
        # 1. Filter picks to known stations and assign sigmas.
        usable: list[tuple[Pick, tuple[float, float]]] = []
        for pick in picks:
            if pick.station_id not in self.stations:
                continue
            if pick.sigma_s is None:
                sig = sigma_p if pick.phase == "P" else sigma_s
                pick = Pick(
                    station_id=pick.station_id,
                    phase=pick.phase,
                    time_s=pick.time_s,
                    sigma_s=sig,
                )
            usable.append((pick, self._station_xy[pick.station_id]))
        if len(usable) < min_picks:
            raise ValueError(
                f"need at least {min_picks} usable picks, got {len(usable)}"
            )

        # 2. Default search centre = station-weighted pick centroid.
        if center_xy_km is None:
            xs = np.array([xy[0] for _, xy in usable])
            ys = np.array([xy[1] for _, xy in usable])
            center_xy_km = (float(xs.mean()), float(ys.mean()))

        cx, cy = center_xy_km
        x_axis = np.arange(
            cx - x_half_width_km,
            cx + x_half_width_km + 0.5 * horizontal_spacing_km,
            horizontal_spacing_km,
        )
        y_axis = np.arange(
            cy - y_half_width_km,
            cy + y_half_width_km + 0.5 * horizontal_spacing_km,
            horizontal_spacing_km,
        )
        z_axis = np.arange(
            depth_min_km,
            depth_max_km + 0.5 * depth_spacing_km,
            depth_spacing_km,
        )
        Nx, Ny, Nz = len(x_axis), len(y_axis), len(z_axis)
        Xg, Yg = np.meshgrid(x_axis, y_axis, indexing="ij")  # (Nx, Ny)

        # 3. Per-pick cached arrays for this grid.
        cached: list[dict] = []
        for pick, (x_sta, y_sta) in usable:
            dist_2d = np.hypot(Xg - x_sta, Yg - y_sta)
            tt_table = self._tt[(pick.station_id, pick.phase)]
            # Per-depth slabs: shape (Nz, Nx, Ny).
            # We compute lazily per depth to reduce peak memory, but here
            # it's small enough to allocate all at once: (Nz, Nx, Ny) * 8B.
            tt_3d = np.empty((Nz, Nx, Ny), dtype=float)
            for zi, z in enumerate(z_axis):
                tt_3d[zi] = tt_table.lookup(z, dist_2d)
            sigma = pick.sigma_s  # guaranteed not None after filtering
            cached.append(
                {
                    "pick": pick,
                    "dist_2d": dist_2d,
                    "tt_3d": tt_3d,
                    "sigma": sigma,
                    "w": 1.0 / (sigma * sigma),
                    "obs": pick.time_s,
                }
            )

        # 4. Compute chi2 over the grid.
        # First, per-grid-node weighted-LS origin time t_0.
        wsum = np.zeros((Nz, Nx, Ny), dtype=float)
        wtsum = np.zeros((Nz, Nx, Ny), dtype=float)
        n_valid = np.zeros((Nz, Nx, Ny), dtype=int)
        for c in cached:
            tt = c["tt_3d"]
            valid = ~np.isnan(tt)
            # Where invalid (super-critical), contribute zero weight.
            w = np.where(valid, c["w"], 0.0)
            wsum += w
            wtsum += w * (c["obs"] - np.where(valid, tt, 0.0))
            n_valid += valid.astype(int)

        # Avoid division by zero.
        safe_w = np.where(wsum > 0, wsum, 1.0)
        t0_grid = wtsum / safe_w

        # Second pass: chi2.
        chi2 = np.zeros((Nz, Nx, Ny), dtype=float)
        for c in cached:
            tt = c["tt_3d"]
            valid = ~np.isnan(tt)
            res = c["obs"] - t0_grid - tt
            chi2 += np.where(valid, (res * res) * c["w"], 0.0)

        # Penalise nodes where insufficient picks were usable.
        required_valid = max(min_picks, 4)
        chi2 = np.where(n_valid < required_valid, np.inf, chi2)

        if not np.isfinite(chi2).any():
            raise RuntimeError(
                "no grid node had enough reachable stations — "
                "try widening the search volume or a different velocity model"
            )

        # 5. Locate minimum.
        flat_idx = int(np.argmin(chi2))
        iz, ix, iy = np.unravel_index(flat_idx, chi2.shape)
        chi2_min = float(chi2[iz, ix, iy])
        best_x = float(x_axis[ix])
        best_y = float(y_axis[iy])
        best_z = float(z_axis[iz])
        best_t0 = float(t0_grid[iz, ix, iy])

        # 6. Convert best (x, y) → (lat, lon).
        lon, lat = km_to_latlon(best_x, best_y, proj=self._proj)

        # 7. Posterior and covariance.
        cov_xyz, sigma_z, h_major, h_minor, h_az = _posterior_uncertainty(
            chi2, chi2_min, x_axis, y_axis, z_axis
        )

        # 8. Residuals at the best node.
        residuals: list[Residual] = []
        sqsum_residual = 0.0
        n_used = 0
        for c in cached:
            tt_best = float(c["tt_3d"][iz, ix, iy])
            if np.isnan(tt_best):
                continue
            obs = c["obs"]
            pred = best_t0 + tt_best
            r = obs - pred
            residuals.append(
                Residual(
                    station_id=c["pick"].station_id,
                    phase=c["pick"].phase,
                    observed_s=obs,
                    predicted_s=pred,
                    residual_s=r,
                    weight=c["w"],
                )
            )
            sqsum_residual += r * r
            n_used += 1
        rms = float(np.sqrt(sqsum_residual / n_used)) if n_used else float("nan")

        return LocationResult(
            latitude=float(lat),
            longitude=float(lon),
            depth_km=best_z,
            origin_time_s=best_t0,
            n_picks_used=n_used,
            rms_residual_s=rms,
            chi2_min=chi2_min,
            covariance_xyz_km2=cov_xyz,
            horizontal_semi_axis_major_km=h_major,
            horizontal_semi_axis_minor_km=h_minor,
            horizontal_semi_axis_azimuth_deg=h_az,
            sigma_depth_km=sigma_z,
            residuals=tuple(residuals),
        )

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def projection_center(self) -> tuple[float, float]:
        """(lat, lon) of the local cartesian origin used internally."""
        return self._proj_center

    def station_xy_km(self, station_id: str) -> tuple[float, float]:
        return self._station_xy[station_id]


# ---------------------------------------------------------------------------
# Posterior uncertainty helpers
# ---------------------------------------------------------------------------


def _posterior_uncertainty(
    chi2: np.ndarray,
    chi2_min: float,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    z_axis: np.ndarray,
) -> tuple[np.ndarray, float, float, float, float]:
    """Compute the posterior covariance and derived confidence measures.

    ``chi2`` is (Nz, Nx, Ny); axes give the local cartesian grid.
    Returns ``(cov_3x3, sigma_z, h_major_km, h_minor_km, h_az_deg)`` where
    the ellipse semi-axes are the **95% confidence** values in the
    horizontal plane (projected covariance).
    """
    # Unnormalised posterior.
    post = np.exp(-0.5 * (chi2 - chi2_min))
    # Replace inf-induced zeros cleanly (already handled, but be safe).
    post = np.where(np.isfinite(post), post, 0.0)
    total = post.sum()
    if total <= 0 or not np.isfinite(total):
        # Degenerate — return NaNs.
        nan3 = np.full((3, 3), np.nan)
        return nan3, float("nan"), float("nan"), float("nan"), float("nan")
    post /= total

    # Marginalise to compute means and covariances on the x, y, z axes.
    # post has shape (Nz, Nx, Ny).
    Zg = z_axis[:, None, None]  # (Nz, 1, 1)
    Xg = x_axis[None, :, None]  # (1, Nx, 1)
    Yg = y_axis[None, None, :]  # (1, 1, Ny)

    mean_x = float((post * Xg).sum())
    mean_y = float((post * Yg).sum())
    mean_z = float((post * Zg).sum())

    dx = Xg - mean_x
    dy = Yg - mean_y
    dz = Zg - mean_z

    cov_xx = float((post * dx * dx).sum())
    cov_yy = float((post * dy * dy).sum())
    cov_zz = float((post * dz * dz).sum())
    cov_xy = float((post * dx * dy).sum())
    cov_xz = float((post * dx * dz).sum())
    cov_yz = float((post * dy * dz).sum())

    cov = np.array(
        [
            [cov_xx, cov_xy, cov_xz],
            [cov_xy, cov_yy, cov_yz],
            [cov_xz, cov_yz, cov_zz],
        ]
    )

    sigma_z = float(np.sqrt(max(cov_zz, 0.0)))

    # Horizontal ellipse: eigendecompose 2x2 block. 95% semi-axes in a 2D
    # Gaussian posterior are sqrt(lambda * chi2.ppf(0.95, df=2)) with
    # chi2.ppf(0.95, df=2) = 5.9915. We report the 2D value because the
    # ellipse is a 2D object; the 3D volume uses a different factor.
    H = cov[:2, :2]
    eigvals, eigvecs = np.linalg.eigh(H)
    # eigh returns ascending eigenvalues.
    lam_minor, lam_major = max(eigvals[0], 0.0), max(eigvals[1], 0.0)
    chi2_95_2d = 5.9915
    h_major = float(np.sqrt(lam_major * chi2_95_2d))
    h_minor = float(np.sqrt(lam_minor * chi2_95_2d))
    # Azimuth of major axis: direction of the eigenvector with largest
    # eigenvalue, measured clockwise from +y (north) in degrees.
    vx, vy = float(eigvecs[0, 1]), float(eigvecs[1, 1])
    az = float(np.degrees(np.arctan2(vx, vy)) % 360.0)

    return cov, sigma_z, h_major, h_minor, az
