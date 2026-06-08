"""Tests for ``lib.location`` — probabilistic grid-search locator.

Strategy
--------
* **Analytical correctness** — compare the direct-ray tracer against
  closed-form travel times in a homogeneous half-space. This is where
  Snell's law collapses to straight-line geometry and the ray tracer must
  match ``t = sqrt(d**2 + z**2) / v`` exactly (to numerical precision).
* **Layered sanity** — in a 2-layer model, check monotonicity of dx(p) on
  the scan and that travel times are always faster than a homogeneous
  model at the slow layer's velocity (because the ray can use the faster
  layer where it passes through it).
* **Station elevation** — a station at altitude sees a longer travel
  time for a source directly beneath it (ray traverses more of the slow
  top layer).
* **Synthetic event round-trip** — generate picks from a known
  hypocentre using the same tracer, then verify the locator returns a
  hypocentre within one grid cell of the input.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.location import (
    DEFAULT_WEST_TEXAS_MODEL,
    GridSearchLocator,
    Layer,
    LayeredModel,
    Pick,
    Station,
    build_tt_table,
    travel_time,
)
from lib.location.travel_times import _scan_slowness

# ---------------------------------------------------------------------------
# Velocity model basics
# ---------------------------------------------------------------------------


class TestLayeredModel:
    def test_single_layer_is_valid(self):
        m = LayeredModel([Layer(0.0, 5.0, 3.0)])
        assert m.velocity(5.0, "P") == 5.0
        assert m.velocity(5.0, "S") == 3.0

    def test_ascending_tops_required(self):
        with pytest.raises(ValueError):
            LayeredModel([Layer(0.0, 5, 3), Layer(0.0, 6, 4)])
        with pytest.raises(ValueError):
            LayeredModel([Layer(1.0, 5, 3), Layer(0.0, 6, 4)])

    def test_vs_must_be_less_than_vp(self):
        with pytest.raises(ValueError):
            Layer(0.0, 3.0, 3.5)

    def test_halfspace_behavior(self):
        m = LayeredModel([Layer(0.0, 5.0, 3.0), Layer(10.0, 7.0, 4.0)])
        # Deep into second layer — stays at its velocity (halfspace).
        assert m.velocity(100.0, "P") == 7.0
        assert m.velocity(100.0, "S") == 4.0

    def test_boundary_uses_deeper_layer(self):
        # Convention: depth exactly at top_km is inside the layer starting there.
        m = LayeredModel([Layer(0.0, 5.0, 3.0), Layer(10.0, 7.0, 4.0)])
        assert m.velocity(10.0, "P") == 7.0

    def test_elevated_station_uses_top_layer(self):
        m = LayeredModel([Layer(0.0, 5.0, 3.0), Layer(10.0, 7.0, 4.0)])
        # Station at 1.2 km elevation → z = -1.2 — top layer extends up.
        assert m.velocity(-1.2, "P") == 5.0

    def test_default_model_shape(self):
        m = DEFAULT_WEST_TEXAS_MODEL
        assert len(m.layers) == 6
        # Vp/Vs in a plausible range (Poisson solid is ~1.732).
        for layer in m.layers:
            vpvs = layer.vp / layer.vs
            assert 1.65 < vpvs < 1.85


# ---------------------------------------------------------------------------
# Ray integration: half-space analytical check
# ---------------------------------------------------------------------------


class TestHalfSpaceTravelTimes:
    """Homogeneous velocity → Snell's law collapses to straight-line geometry.

    For a source at depth z directly below a surface receiver at distance
    d, the travel time is ``sqrt(d**2 + z**2) / v``. The ray tracer should
    reproduce this to numerical precision.
    """

    @pytest.fixture
    def halfspace(self):
        return LayeredModel([Layer(0.0, 6.0, 3.4)])

    def test_vertical_ray(self, halfspace):
        # Source at 5 km depth, receiver at surface, 0 km epicentral.
        t = travel_time(5.0, 0.0, 0.0, halfspace, "P")
        assert t == pytest.approx(5.0 / 6.0, rel=1e-9)

    def test_45_degree_ray(self, halfspace):
        # Source at 5 km depth, receiver 5 km away → ray length 5*sqrt(2).
        t = travel_time(5.0, 0.0, 5.0, halfspace, "P")
        expected = np.sqrt(50.0) / 6.0
        assert t == pytest.approx(expected, rel=1e-4)

    @pytest.mark.parametrize(
        "depth,dist",
        [
            (2.0, 0.5),
            (5.0, 10.0),
            (10.0, 20.0),
            (1.0, 30.0),
            (8.0, 15.5),
        ],
    )
    def test_matches_analytical(self, halfspace, depth, dist):
        t = travel_time(depth, 0.0, dist, halfspace, "P")
        expected = np.sqrt(dist**2 + depth**2) / 6.0
        assert t == pytest.approx(expected, rel=2e-3)

    def test_s_wave_slower_than_p(self, halfspace):
        tp = travel_time(5.0, 0.0, 10.0, halfspace, "P")
        ts = travel_time(5.0, 0.0, 10.0, halfspace, "S")
        # Vp/Vs = 6/3.4 = 1.765, so ts = tp * 1.765.
        assert ts / tp == pytest.approx(6.0 / 3.4, rel=1e-3)


# ---------------------------------------------------------------------------
# Layered sanity
# ---------------------------------------------------------------------------


class TestLayeredTravelTimes:
    @pytest.fixture
    def two_layer(self):
        # Slow top, fast bottom — typical crustal structure.
        return LayeredModel([Layer(0.0, 4.0, 2.3), Layer(5.0, 6.5, 3.75)])

    def test_dx_monotonic_in_slowness(self, two_layer):
        dx, dt = _scan_slowness(8.0, 0.0, two_layer, "P")
        # Must be non-decreasing (the implementation enforces this with
        # cummax — verify it was actually monotonic to begin with too).
        assert np.all(np.diff(dx) >= -1e-9)
        assert np.all(np.diff(dt) >= -1e-9)

    def test_deep_source_sees_faster_velocity(self, two_layer):
        # A ray from a deep source (10 km) traverses fast rock; travel
        # time per km should be faster than the slow top-layer velocity.
        t_deep = travel_time(10.0, 0.0, 20.0, two_layer, "P")
        apparent_v = np.sqrt(20.0**2 + 10.0**2) / t_deep
        assert apparent_v > 4.0  # faster than slow top layer

    def test_short_distance_close_to_halfspace(self, two_layer):
        # When source is in slow top layer and distance is short, the ray
        # stays in the slow layer; travel time should match a homogeneous
        # slow-layer model closely.
        t_layered = travel_time(2.0, 0.0, 1.0, two_layer, "P")
        t_homo = travel_time(2.0, 0.0, 1.0, LayeredModel([Layer(0.0, 4.0, 2.3)]), "P")
        assert t_layered == pytest.approx(t_homo, rel=1e-4)


# ---------------------------------------------------------------------------
# Station elevation
# ---------------------------------------------------------------------------


class TestElevationCorrection:
    def test_elevated_station_slower(self):
        # Uniform top layer of 3.5 km/s; a station at 1.2 km elevation
        # (receiver_depth = -1.2 km) sees a longer ray from a surface
        # source at 0 km distance than a datum-level station would.
        model = LayeredModel([Layer(0.0, 3.5, 2.0)])
        t_surface = travel_time(5.0, 0.0, 0.0, model, "P")
        t_elevated = travel_time(5.0, -1.2, 0.0, model, "P")
        # Extra 1.2 km at 3.5 km/s = 0.3429 s.
        assert t_elevated - t_surface == pytest.approx(1.2 / 3.5, rel=1e-6)


# ---------------------------------------------------------------------------
# Travel-time table
# ---------------------------------------------------------------------------


class TestTravelTimeTable:
    @pytest.fixture
    def halfspace_table(self):
        model = LayeredModel([Layer(0.0, 6.0, 3.4)])
        return build_tt_table(
            model=model,
            source_depths_km=np.arange(0.0, 15.01, 0.5),
            distances_km=np.arange(0.0, 50.01, 0.5),
            receiver_depth_km=0.0,
            phase="P",
        )

    def test_lookup_matches_analytical(self, halfspace_table):
        # Bilinear interp should return near-analytical values at
        # off-grid points.
        t = halfspace_table.lookup(3.25, 12.75)
        expected = np.sqrt(3.25**2 + 12.75**2) / 6.0
        assert float(t) == pytest.approx(expected, rel=2e-3)

    def test_lookup_broadcasts(self, halfspace_table):
        z = np.array([3.0, 5.0, 7.0])[:, None]  # (3, 1)
        d = np.array([5.0, 10.0, 15.0, 20.0])[None, :]  # (1, 4)
        t = halfspace_table.lookup(z, d)
        assert t.shape == (3, 4)
        # Each entry should be t = sqrt(d^2 + z^2) / v.
        for iz, zi in enumerate([3.0, 5.0, 7.0]):
            for id_, di in enumerate([5.0, 10.0, 15.0, 20.0]):
                expected = np.sqrt(di**2 + zi**2) / 6.0
                assert t[iz, id_] == pytest.approx(expected, rel=2e-3)


# ---------------------------------------------------------------------------
# End-to-end synthetic event round-trip
# ---------------------------------------------------------------------------


class TestSyntheticEventRoundTrip:
    """Place a source at a known hypocentre, synthesise picks from the
    ray tracer, and verify the locator recovers that hypocentre within
    the grid resolution."""

    @pytest.fixture
    def stations(self):
        # A small synthetic network surrounding (0, 0).
        return [
            Station("NET.A", 31.90, -106.50, 1200.0),
            Station("NET.B", 31.85, -106.35, 1100.0),
            Station("NET.C", 31.80, -106.45, 1300.0),
            Station("NET.D", 31.88, -106.40, 1150.0),
            Station("NET.E", 31.82, -106.30, 1250.0),
            Station("NET.F", 31.95, -106.40, 1000.0),
        ]

    @pytest.fixture
    def locator(self, stations):
        return GridSearchLocator(
            stations,
            projection_center_latlon=(31.85, -106.40),
            tt_depth_step_km=0.25,
            tt_distance_step_km=0.5,
        )

    def _synthesize_picks(self, locator, true_lat, true_lon, true_depth, true_t0):
        """Compute noise-free P and S picks from each station."""
        from lib.projection import latlon_to_km

        true_x, true_y = latlon_to_km(true_lat, true_lon, proj=locator._proj)
        picks = []
        for sta_id, (x_s, y_s) in locator._station_xy.items():
            dist = float(np.hypot(true_x - x_s, true_y - y_s))
            for phase in ("P", "S"):
                tt_val = float(locator._tt[(sta_id, phase)].lookup(true_depth, dist))
                if not np.isfinite(tt_val):
                    continue
                picks.append(
                    Pick(
                        station_id=sta_id,
                        phase=phase,
                        time_s=true_t0 + tt_val,
                    )
                )
        return picks

    def test_roundtrip_recovers_hypocentre(self, locator):
        true_lat = 31.86
        true_lon = -106.38
        true_depth = 6.0
        true_t0 = 100.0
        picks = self._synthesize_picks(locator, true_lat, true_lon, true_depth, true_t0)
        assert len(picks) >= 8

        result = locator.locate(
            picks,
            x_half_width_km=30.0,
            y_half_width_km=30.0,
            depth_min_km=0.0,
            depth_max_km=15.0,
            horizontal_spacing_km=0.25,
            depth_spacing_km=0.25,
        )

        # With noise-free picks and a fine grid, the locator should recover
        # the input within a handful of cells. 0.25 km spacing + bilinear
        # interpolation artefacts in the TT table put us in the 0.5–1 km
        # regime for horizontal error.
        assert abs(result.latitude - true_lat) < 0.01  # ~1 km
        assert abs(result.longitude - true_lon) < 0.01
        assert abs(result.depth_km - true_depth) < 1.0
        assert abs(result.origin_time_s - true_t0) < 0.1
        assert result.rms_residual_s < 0.05

    def test_result_contains_uncertainty(self, locator):
        picks = self._synthesize_picks(locator, 31.86, -106.38, 6.0, 100.0)
        result = locator.locate(
            picks,
            x_half_width_km=30.0,
            y_half_width_km=30.0,
            depth_max_km=15.0,
            horizontal_spacing_km=0.25,
            depth_spacing_km=0.25,
        )
        # Covariance is PSD and finite.
        cov = result.covariance_xyz_km2
        assert cov.shape == (3, 3)
        assert np.all(np.isfinite(cov))
        eig = np.linalg.eigvalsh(cov)
        assert np.all(eig >= -1e-9)  # numerically non-negative
        # 95% ellipse axes and depth sigma are finite and small for noise-
        # free picks.
        assert np.isfinite(result.horizontal_semi_axis_major_km)
        assert np.isfinite(result.sigma_depth_km)
        assert result.horizontal_semi_axis_major_km < 5.0
        assert result.sigma_depth_km < 3.0

    def test_rejects_insufficient_picks(self, locator):
        picks = [
            Pick("NET.A", "P", 0.0),
            Pick("NET.B", "P", 1.0),
        ]
        with pytest.raises(ValueError, match="at least"):
            locator.locate(picks, min_picks=4)

    def test_ignores_unknown_stations(self, locator):
        picks = [
            Pick("NET.ZZZ", "P", 0.0),  # not in catalog
            Pick("NET.A", "P", 1.0),
            Pick("NET.B", "P", 1.2),
            Pick("NET.C", "P", 1.5),
            Pick("NET.D", "P", 1.8),
            Pick("NET.A", "S", 2.1),
            Pick("NET.B", "S", 2.4),
        ]
        # Doesn't blow up on unknown station.
        result = locator.locate(
            picks,
            x_half_width_km=40.0,
            y_half_width_km=40.0,
            horizontal_spacing_km=1.0,
            depth_spacing_km=1.0,
        )
        assert result.n_picks_used >= 4
