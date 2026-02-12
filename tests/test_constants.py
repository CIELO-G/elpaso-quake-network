"""Tests for lib/constants.py — path construction and constants."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.constants import (
    NETWORK_CENTER_LAT,
    NETWORK_CENTER_LON,
    daily_events_paths,
    daily_picks_path,
    raw_file_name,
)


class TestConstants:
    def test_network_center_reasonable(self):
        assert 31.0 < NETWORK_CENTER_LAT < 33.0
        assert -107.0 < NETWORK_CENTER_LON < -106.0


class TestRawFileName:
    def test_basic(self):
        name = raw_file_name("AM", "R0F2D", "00", "EHZ", "2026", "029")
        assert name == "AM.R0F2D.00.EHZ.2026.029.mseed"


class TestDailyPicksPath:
    def test_basic(self):
        p = daily_picks_path("output", "2026", "029")
        assert str(p) == "output/3-picks/2026/029/2026.029.picks.csv"


class TestDailyEventsPaths:
    def test_basic(self):
        events, assignments = daily_events_paths("output", "2026", "029")
        assert str(events) == "output/4-events/2026/029/2026.029.events.csv"
        assert str(assignments) == "output/4-events/2026/029/2026.029.assignments.csv"
