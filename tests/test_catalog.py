"""Tests for catalog.py — event ID generation and catalog logic."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import from catalog module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "5-catalog"))
from catalog import _jday_to_date, day_key, make_event_id


class TestJdayToDate:
    def test_jan_1(self):
        assert _jday_to_date("2026", "001") == "20260101"

    def test_feb_15(self):
        assert _jday_to_date("2026", "046") == "20260215"

    def test_dec_31(self):
        assert _jday_to_date("2026", "365") == "20261231"


class TestMakeEventId:
    def test_basic(self):
        eid = make_event_id("2026", "029", 1)
        assert eid == "ep20260129-0001"

    def test_padding(self):
        eid = make_event_id("2026", "001", 42)
        assert eid == "ep20260101-0042"

    def test_large_index(self):
        eid = make_event_id("2026", "365", 9999)
        assert eid == "ep20261231-9999"


class TestDayKey:
    def test_basic(self):
        assert day_key("2026", "029") == "20260129"
