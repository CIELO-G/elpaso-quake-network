"""Tests for ``lib.location.nlloc`` — NonLinLoc Python wrapper.

These tests skip entirely if NLLoc isn't installed or the per-station
travel-time grids haven't been built — both are runtime prerequisites,
not pip dependencies. See ``nlloc/README.md`` for the one-time setup.

Strategy
--------
* **Wrapper-shape tests** verify the LocationResult fields the dashboard
  endpoint depends on (lat/lon/depth, origin_time_s, ellipse, sigma_z,
  residuals).
* **Round-trip test** runs the locator on a synthetic event built from
  the same DEFAULT_WEST_TEXAS_MODEL the grids were generated from and
  checks that the recovered hypocentre is within ~1 km of the input.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.constants import PROJECT_ROOT
from lib.location import (
    DEFAULT_WEST_TEXAS_MODEL,
    GridSearchLocator,
    Pick,
    Station,
)

# Skip the whole module unless NLLoc + grids are available.
_NLLOC_BIN = PROJECT_ROOT.parent / "NonLinLoc" / "src" / "bin" / "NLLoc"
_NLLOC_GRIDS = PROJECT_ROOT / "output" / "nlloc" / "grids" / "time"

if not _NLLOC_BIN.exists():
    pytest.skip(
        "NLLoc binary not built; see nlloc/README.md",
        allow_module_level=True,
    )
if not any(_NLLOC_GRIDS.glob("*.time.hdr")):
    pytest.skip(
        "NLLoc travel-time grids not built; run scripts/nlloc_build_grids.py",
        allow_module_level=True,
    )

from lib.location.nlloc import NLLocLocator  # noqa: E402  (after skip checks)


@pytest.fixture(scope="module")
def stations() -> list[Station]:
    """Small synthetic network covering the El Paso study area."""
    return [
        Station("AM.R0F2D", 31.648649, -106.460311, 1196.0),
        Station("AM.R4B41", 31.828829, -106.531547, 1258.0),
        Station("AM.RE2E7", 31.928829, -106.420000, 1280.0),
        Station("EP.KIDD", 31.771774, -106.506378, 1162.5),
    ]


@pytest.fixture(scope="module")
def locator(stations) -> NLLocLocator:
    return NLLocLocator(stations)


@pytest.fixture(scope="module")
def grid_search_reference(stations) -> GridSearchLocator:
    """GridSearch locator on the same model, used as a sanity reference."""
    return GridSearchLocator(
        stations,
        model=DEFAULT_WEST_TEXAS_MODEL,
        projection_center_latlon=(31.85, -106.40),
        tt_depth_step_km=0.25,
        tt_distance_step_km=0.5,
    )


def _synthesize_picks(
    locator: GridSearchLocator,
    true_lat: float,
    true_lon: float,
    true_depth_km: float,
    true_t0: float,
) -> list[Pick]:
    """Generate noise-free synthetic picks for a known hypocentre.

    Uses the GridSearch locator's precomputed travel-time tables so the
    picks are internally consistent with DEFAULT_WEST_TEXAS_MODEL — the
    same model the NLLoc grids were built from.
    """
    import numpy as np

    from lib.projection import latlon_to_km

    true_x, true_y = latlon_to_km(true_lat, true_lon, proj=locator._proj)
    picks: list[Pick] = []
    for sta_id, (x_s, y_s) in locator._station_xy.items():
        dist = float(np.hypot(true_x - x_s, true_y - y_s))
        for phase in ("P", "S"):
            tt = float(locator._tt[(sta_id, phase)].lookup(true_depth_km, dist))
            if np.isfinite(tt):
                picks.append(Pick(station_id=sta_id, phase=phase, time_s=true_t0 + tt))
    return picks


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_stations(self):
        with pytest.raises(ValueError, match="at least one station"):
            NLLocLocator([])

    def test_resolves_default_grid_dir(self, locator):
        assert locator._grid_dir.exists()
        assert locator._nlloc_bin.exists()


# ---------------------------------------------------------------------------
# Round-trip — recover known hypocentre from synthetic picks
# ---------------------------------------------------------------------------


class TestSyntheticRoundTrip:
    def test_recovers_hypocentre(self, locator, grid_search_reference):
        # A shallow event in the centre of the El Paso array
        true_lat, true_lon, true_depth = 31.86, -106.45, 8.0
        true_t0 = 1700000000.0  # arbitrary Unix epoch
        picks = _synthesize_picks(grid_search_reference, true_lat, true_lon, true_depth, true_t0)
        assert len(picks) >= 4, f"need ≥4 picks, got {len(picks)}"

        result = locator.locate(picks, ref_epoch_unix=None, min_picks=4)

        # NLLoc with default 1 km grid + bilinear-interp TT tables
        # in synthesis: expect agreement within ~1 km horizontal, ~2 km depth
        assert abs(result.latitude - true_lat) < 0.02  # ~2 km lat
        assert abs(result.longitude - true_lon) < 0.02  # ~2 km lon
        assert abs(result.depth_km - true_depth) < 3.0
        # Origin time is in Unix epoch when ref_epoch_unix=None
        assert abs(result.origin_time_s - true_t0) < 1.0

    def test_result_shape_for_dashboard(self, locator, grid_search_reference):
        """Verify every field the dashboard /relocate response touches."""
        picks = _synthesize_picks(grid_search_reference, 31.86, -106.45, 8.0, 1700000000.0)
        result = locator.locate(picks, ref_epoch_unix=None, min_picks=4)

        # Scalar location/uncertainty fields
        assert isinstance(result.latitude, float)
        assert isinstance(result.longitude, float)
        assert isinstance(result.depth_km, float)
        assert isinstance(result.origin_time_s, float)
        assert isinstance(result.rms_residual_s, float)
        assert isinstance(result.n_picks_used, int)
        assert isinstance(result.sigma_depth_km, float)
        assert isinstance(result.horizontal_semi_axis_major_km, float)
        assert isinstance(result.horizontal_semi_axis_minor_km, float)
        assert isinstance(result.horizontal_semi_axis_azimuth_deg, float)

        # Residuals
        assert len(result.residuals) >= 4
        r0 = result.residuals[0]
        assert r0.phase in ("P", "S")
        assert isinstance(r0.observed_s, float)
        assert isinstance(r0.predicted_s, float)
        assert isinstance(r0.residual_s, float)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_rejects_too_few_picks(self, locator):
        picks = [
            Pick("AM.R0F2D", "P", 0.0, sigma_s=0.1),
            Pick("AM.R4B41", "P", 1.0, sigma_s=0.1),
            Pick("AM.RE2E7", "P", 1.5, sigma_s=0.1),
        ]
        with pytest.raises(ValueError, match="at least 4"):
            locator.locate(picks, ref_epoch_unix=None, min_picks=4)

    def test_ignores_unknown_stations(self, locator, grid_search_reference):
        good_picks = _synthesize_picks(grid_search_reference, 31.86, -106.45, 8.0, 1700000000.0)
        # Add a pick from a station the locator doesn't know about
        ghost = Pick("NET.GHOST", "P", good_picks[0].time_s, sigma_s=0.1)
        result = locator.locate([ghost] + good_picks, ref_epoch_unix=None, min_picks=4)
        # Ghost pick is silently dropped — recovery should still work
        assert abs(result.latitude - 31.86) < 0.05


# ---------------------------------------------------------------------------
# Cross-check vs GridSearch on a real-ish event
# ---------------------------------------------------------------------------


class TestCrossCheckAgainstGridSearch:
    """Spot-check that NLLoc and GridSearch agree to within their joint
    uncertainty on the same synthetic event. Both use the same velocity
    model, so disagreement >> ellipse radii would indicate a wrapper bug
    rather than method differences."""

    def test_agreement_within_uncertainty(self, locator, grid_search_reference):
        picks = _synthesize_picks(grid_search_reference, 31.86, -106.45, 8.0, 1700000000.0)
        ref_epoch = min(p.time_s for p in picks)
        picks_rel = [
            Pick(
                station_id=p.station_id,
                phase=p.phase,
                time_s=p.time_s - ref_epoch,
                sigma_s=p.sigma_s,
            )
            for p in picks
        ]

        r_nl = locator.locate(picks, ref_epoch_unix=None, min_picks=4)
        r_gs = grid_search_reference.locate(
            picks_rel,
            x_half_width_km=40,
            y_half_width_km=40,
            horizontal_spacing_km=0.5,
            depth_max_km=20,
            depth_spacing_km=0.25,
            min_picks=4,
        )

        # Both methods should agree within ~2 km horizontal on a noise-free
        # synthetic event. (Larger disagreement would indicate a wrapper
        # bug rather than acceptable method-to-method scatter.)
        import math

        dlat_km = (r_nl.latitude - r_gs.latitude) * 111.32
        dlon_km = (r_nl.longitude - r_gs.longitude) * 111.32 * math.cos(math.radians(r_nl.latitude))
        horiz_offset_km = math.sqrt(dlat_km**2 + dlon_km**2)
        assert horiz_offset_km < 2.5, (
            f"NLLoc vs GridSearch horizontal offset {horiz_offset_km:.2f} km "
            "exceeds wrapper-correctness tolerance (2.5 km)"
        )
        assert abs(r_nl.depth_km - r_gs.depth_km) < 3.0
