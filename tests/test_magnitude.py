"""Tests for lib/magnitude.py — local magnitude computation."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.magnitude import (
    MLConfig,
    compute_ml_network,
    compute_ml_station,
    haversine_km,
    velocity_to_wa_displacement_mm,
)


class TestHaversine:
    """Tests for haversine_km()."""

    def test_zero_distance(self):
        d = haversine_km(31.85, -106.40, 31.85, -106.40)
        assert d == pytest.approx(0.0, abs=0.001)

    def test_known_distance(self):
        # El Paso to Las Cruces (~70 km)
        d = haversine_km(31.76, -106.49, 32.31, -106.75)
        assert 55 < d < 80

    def test_symmetry(self):
        d1 = haversine_km(31.0, -106.0, 32.0, -106.5)
        d2 = haversine_km(32.0, -106.5, 31.0, -106.0)
        assert d1 == pytest.approx(d2)

    def test_equator_one_degree(self):
        d = haversine_km(0.0, 0.0, 0.0, 1.0)
        assert d == pytest.approx(111.19, abs=0.5)


class TestVelocityToWA:
    """Tests for velocity_to_wa_displacement_mm()."""

    def test_positive_amplitude(self):
        result = velocity_to_wa_displacement_mm(1e-6, freq_hz=5.0, wa_gain=2800)
        assert result > 0

    def test_scales_linearly_with_amplitude(self):
        r1 = velocity_to_wa_displacement_mm(1e-6, freq_hz=5.0, wa_gain=2800)
        r2 = velocity_to_wa_displacement_mm(2e-6, freq_hz=5.0, wa_gain=2800)
        assert r2 == pytest.approx(2 * r1)

    def test_inversely_proportional_to_frequency(self):
        r1 = velocity_to_wa_displacement_mm(1e-6, freq_hz=5.0, wa_gain=2800)
        r2 = velocity_to_wa_displacement_mm(1e-6, freq_hz=10.0, wa_gain=2800)
        assert r1 == pytest.approx(2 * r2)


class TestComputeMLStation:
    """Tests for compute_ml_station()."""

    def test_returns_float_for_valid_input(self):
        ml = compute_ml_station(1e-5, 50.0)
        assert isinstance(ml, float)

    def test_returns_none_for_zero_amplitude(self):
        assert compute_ml_station(0.0, 50.0) is None

    def test_returns_none_for_negative_amplitude(self):
        assert compute_ml_station(-1e-5, 50.0) is None

    def test_returns_none_below_min_distance(self):
        cfg = MLConfig(min_distance_km=10.0)
        assert compute_ml_station(1e-5, 5.0, cfg) is None

    def test_magnitude_increases_with_amplitude(self):
        ml1 = compute_ml_station(1e-6, 50.0)
        ml2 = compute_ml_station(1e-5, 50.0)
        assert ml2 > ml1

    def test_magnitude_increases_with_distance(self):
        # For the same amplitude, a more distant station implies larger event
        ml1 = compute_ml_station(1e-5, 20.0)
        ml2 = compute_ml_station(1e-5, 80.0)
        assert ml2 > ml1

    def test_custom_config(self):
        cfg = MLConfig(
            freq_hz=5.0, wa_gain=2800, min_distance_km=10.0,
            a=1.110, b=0.00189, c=3.0, ref_distance_km=100.0,
        )
        ml = compute_ml_station(1e-5, 50.0, cfg)
        assert isinstance(ml, float)
        assert -2 < ml < 6  # Sanity range


class TestComputeMLNetwork:
    """Tests for compute_ml_network()."""

    def test_empty_returns_none(self):
        ml, err, n = compute_ml_network([])
        assert ml is None
        assert err is None
        assert n == 0

    def test_single_reading(self):
        ml, err, n = compute_ml_network([2.5])
        assert ml == pytest.approx(2.5)
        assert err is None  # Can't compute error from 1 reading
        assert n == 1

    def test_multiple_readings(self):
        readings = [2.0, 2.5, 3.0]
        ml, err, n = compute_ml_network(readings)
        assert ml == pytest.approx(2.5)  # median
        assert err == pytest.approx(0.5, abs=0.01)  # stdev
        assert n == 3

    def test_identical_readings(self):
        ml, err, n = compute_ml_network([2.0, 2.0, 2.0])
        assert ml == pytest.approx(2.0)
        assert err == pytest.approx(0.0)
