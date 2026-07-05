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


# ── Content-aware reviewed-event restore (duplicate-event bug fix) ────
import logging

import pandas as pd

from catalog import _same_event, restore_reviewed_events, warn_near_duplicates

_log = logging.getLogger("test_catalog")


def _cat(rows):
    cols = ["event_id", "time", "latitude", "longitude", "magnitude",
            "reviewed", "review_status", "event_type"]
    return pd.DataFrame(rows, columns=cols).fillna("")


REVIEWED = {
    "event_id": "ep20260121-0008",
    "time": "2026-01-21T00:13:39.509260Z",
    "latitude": 31.682431,
    "longitude": -106.4161,
    "magnitude": 1.32,
    "reviewed": "2026-06-07T21:45:56+00:00",
    "review_status": "confirmed",
    "event_type": "quarry_blast",
}


class TestSameEvent:
    def test_identical(self):
        assert _same_event("2026-01-21T00:13:39.5Z", 31.68, -106.41,
                           "2026-01-21T00:13:39.5Z", 31.68, -106.41)

    def test_close_in_time_and_space(self):
        assert _same_event("2026-01-21T00:13:39.5Z", 31.68, -106.41,
                           "2026-01-21T00:13:40.9Z", 31.69, -106.42)

    def test_far_in_time(self):
        assert not _same_event("2026-01-21T00:13:39.5Z", 31.68, -106.41,
                               "2026-01-21T00:13:45.0Z", 31.68, -106.41)

    def test_far_in_space(self):
        assert not _same_event("2026-01-21T00:13:39.5Z", 31.68, -106.41,
                               "2026-01-21T00:13:39.5Z", 31.90, -106.41)

    def test_naive_vs_z_suffix(self):
        assert _same_event("2026-01-21T00:13:39.5", 31.68, -106.41,
                           "2026-01-21T00:13:39.5Z", 31.68, -106.41)


class TestRestoreReviewedEvents:
    def test_in_place_when_id_and_content_match(self):
        catalog = _cat([{**REVIEWED, "reviewed": "", "review_status": "",
                         "event_type": "", "latitude": 31.6825}])
        out, _ = restore_reviewed_events(
            catalog, pd.DataFrame(), {"ep20260121-0008": REVIEWED}, {}, _log)
        assert len(out) == 1
        assert out.iloc[0]["review_status"] == "confirmed"
        assert float(out.iloc[0]["latitude"]) == REVIEWED["latitude"]

    def test_orphan_reclaims_reassociated_twin(self):
        # Same physical event came back under a new id, unreviewed.
        twin = {**REVIEWED, "event_id": "ep20260121-0002",
                "time": "2026-01-21T00:13:39.692", "reviewed": "",
                "review_status": "", "event_type": ""}
        catalog = _cat([twin])
        assigns = pd.DataFrame([{"event_id": "ep20260121-0002", "station": "R0F2D"}])
        out, out_assigns = restore_reviewed_events(
            catalog, assigns, {"ep20260121-0008": REVIEWED},
            {"ep20260121-0008": [{"event_id": "ep20260121-0008", "station": "R0F2D"}]},
            _log)
        # Twin dropped, reviewed row kept — exactly one row for the event.
        assert list(out["event_id"]) == ["ep20260121-0008"]
        assert list(out_assigns["event_id"].unique()) == ["ep20260121-0008"]

    def test_orphan_without_twin_is_appended(self):
        other = {**REVIEWED, "event_id": "ep20260122-0000",
                 "time": "2026-01-22T10:00:00.000Z", "reviewed": "",
                 "review_status": "", "event_type": ""}
        out, _ = restore_reviewed_events(
            _cat([other]), pd.DataFrame(), {"ep20260121-0008": REVIEWED}, {}, _log)
        assert set(out["event_id"]) == {"ep20260122-0000", "ep20260121-0008"}

    def test_reused_id_not_clobbered(self):
        # A DIFFERENT event now owns the reviewed row's old id.
        different = {**REVIEWED, "event_id": "ep20260121-0008",
                     "time": "2026-01-21T09:00:00.000Z", "reviewed": "",
                     "review_status": "", "event_type": ""}
        out, _ = restore_reviewed_events(
            _cat([different]), pd.DataFrame(), {"ep20260121-0008": REVIEWED}, {}, _log)
        # New event survives untouched; reviewed row restored alongside.
        assert len(out) == 2
        new_row = out[out["time"] == "2026-01-21T09:00:00.000Z"].iloc[0]
        assert str(new_row["review_status"]).strip() == ""
        assert (out["review_status"] == "confirmed").sum() == 1

    def test_never_drops_another_reviewed_row(self):
        # A reviewed near-twin must not be reclaimed/dropped.
        reviewed_twin = {**REVIEWED, "event_id": "ep20260121-0002",
                         "time": "2026-01-21T00:13:39.692"}
        out, _ = restore_reviewed_events(
            _cat([reviewed_twin]), pd.DataFrame(),
            {"ep20260121-0008": REVIEWED}, {}, _log)
        assert set(out["event_id"]) == {"ep20260121-0002", "ep20260121-0008"}


class TestWarnNearDuplicates:
    def test_finds_pair(self):
        twin = {**REVIEWED, "event_id": "ep20260121-0002"}
        assert warn_near_duplicates(_cat([REVIEWED, twin]), _log) == 1

    def test_clean_catalog(self):
        other = {**REVIEWED, "event_id": "ep20260122-0000",
                 "time": "2026-01-22T10:00:00.000Z"}
        assert warn_near_duplicates(_cat([REVIEWED, other]), _log) == 0
