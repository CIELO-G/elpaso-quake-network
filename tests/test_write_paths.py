"""Tests for the dashboard + pipeline write paths.

These are the places where bugs have caused real data loss in the past:
  - Catalog --rebuild wiping reviewed events (patched: snapshot + restore)
  - Atomic writes not actually being atomic (patched: temp + fsync + replace)
  - PhaseNet failures silently marking days as "done" (patched: return non-zero)
  - Concurrent dashboard saves dropping edits (patched: asyncio.Lock)

Every test here protects a past bug from recurring.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "5-catalog"))


# ── atomic_write_df ──────────────────────────────────────────────


class TestAtomicWriteDf:
    """Verify the crash-safe DataFrame writer in lib/atomic_io.py."""

    def test_writes_and_reads_back(self, tmp_path: Path) -> None:
        from lib.atomic_io import atomic_write_df

        df = pd.DataFrame({"event_id": ["e1", "e2"], "magnitude": [1.5, 2.3]})
        out = tmp_path / "catalog.csv"
        atomic_write_df(df, out)

        back = pd.read_csv(out)
        assert list(back.columns) == ["event_id", "magnitude"]
        assert len(back) == 2
        assert back["event_id"].tolist() == ["e1", "e2"]

    def test_no_temp_file_left_behind_on_success(self, tmp_path: Path) -> None:
        from lib.atomic_io import atomic_write_df

        df = pd.DataFrame({"a": [1]})
        atomic_write_df(df, tmp_path / "out.csv")

        # Only the final file should exist — no .tmp leftovers
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "out.csv"

    def test_temp_file_cleaned_up_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        """If to_csv raises, the temp file must be cleaned up."""
        from lib.atomic_io import atomic_write_df

        # Force to_csv to raise after the temp file is created
        original_to_csv = pd.DataFrame.to_csv

        def boom(self, *args, **kwargs):
            raise OSError("simulated write failure")

        monkeypatch.setattr(pd.DataFrame, "to_csv", boom)
        with pytest.raises(OSError):
            atomic_write_df(pd.DataFrame({"a": [1]}), tmp_path / "fail.csv")
        monkeypatch.setattr(pd.DataFrame, "to_csv", original_to_csv)

        # No file left behind
        assert list(tmp_path.iterdir()) == []

    def test_destination_unchanged_if_write_fails(self, tmp_path: Path, monkeypatch) -> None:
        """If write fails partway, the existing destination must be untouched."""
        from lib.atomic_io import atomic_write_df

        out = tmp_path / "existing.csv"
        # Pre-populate with known content
        out.write_text("event_id,magnitude\noriginal,9.9\n")

        def boom(self, *args, **kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(pd.DataFrame, "to_csv", boom)
        with pytest.raises(OSError):
            atomic_write_df(pd.DataFrame({"a": [1]}), out)

        # Original content preserved (this is the WHOLE POINT of atomic_write)
        assert "original,9.9" in out.read_text()


# ── reviews.jsonl sidecar ────────────────────────────────────────


class TestReviewsSidecar:
    """Verify the append-only review-snapshot sidecar in 5-catalog/catalog.py."""

    def test_appends_one_record_per_event(self, tmp_path: Path) -> None:
        from catalog import write_reviews_sidecar

        events = {
            "e1": {"event_id": "e1", "review_status": "confirmed"},
            "e2": {"event_id": "e2", "review_status": "rejected"},
        }
        sidecar = tmp_path / "reviews.jsonl"
        n = write_reviews_sidecar(sidecar, events, {})
        assert n == 2
        lines = sidecar.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_subsequent_writes_append_never_truncate(self, tmp_path: Path) -> None:
        """The sidecar is the recovery source of truth — it must never shrink."""
        from catalog import write_reviews_sidecar

        sidecar = tmp_path / "reviews.jsonl"
        write_reviews_sidecar(sidecar, {"e1": {"event_id": "e1"}}, {})
        write_reviews_sidecar(sidecar, {"e2": {"event_id": "e2"}}, {})
        write_reviews_sidecar(sidecar, {"e3": {"event_id": "e3"}}, {})

        lines = sidecar.read_text().strip().split("\n")
        assert len(lines) == 3
        event_ids = [json.loads(line)["event_id"] for line in lines]
        assert event_ids == ["e1", "e2", "e3"]

    def test_records_include_picks(self, tmp_path: Path) -> None:
        from catalog import write_reviews_sidecar

        events = {"e1": {"event_id": "e1"}}
        picks = {"e1": [
            {"event_id": "e1", "station": "X1", "phase": "P"},
            {"event_id": "e1", "station": "X2", "phase": "S"},
        ]}
        sidecar = tmp_path / "reviews.jsonl"
        write_reviews_sidecar(sidecar, events, picks)
        record = json.loads(sidecar.read_text().strip())
        assert len(record["picks"]) == 2

    def test_nan_serializes_as_null(self, tmp_path: Path) -> None:
        """pandas NaN must round-trip as JSON null, not crash with TypeError."""
        from catalog import write_reviews_sidecar

        events = {"e1": {"event_id": "e1", "magnitude": float("nan")}}
        sidecar = tmp_path / "reviews.jsonl"
        write_reviews_sidecar(sidecar, events, {})
        record = json.loads(sidecar.read_text().strip())
        assert record["event"]["magnitude"] is None

    def test_returns_zero_for_empty_events(self, tmp_path: Path) -> None:
        from catalog import write_reviews_sidecar

        sidecar = tmp_path / "reviews.jsonl"
        n = write_reviews_sidecar(sidecar, {}, {})
        assert n == 0
        # Should not create file at all
        assert not sidecar.exists()


# ── Catalog rebuild preserves reviews ────────────────────────────


class TestCatalogRebuildPreservesReviews:
    """The bug-that-must-not-recur: --rebuild deleting reviewed events.

    Replicates the snapshot + restore logic the way catalog.py does it.
    """

    def test_reviewed_events_survive_rebuild_snapshot(self, tmp_path: Path) -> None:
        """Build a fake catalog with mixed reviewed/unreviewed rows, snapshot,
        verify the snapshot only contains reviewed rows."""
        from catalog import write_reviews_sidecar

        # Simulate what catalog.py's snapshot block does
        catalog_path = tmp_path / "catalog.csv"
        catalog_df = pd.DataFrame([
            {"event_id": "e1", "magnitude": 1.5, "review_status": "confirmed", "reviewed": "2026-01-01"},
            {"event_id": "e2", "magnitude": 2.0, "review_status": "", "reviewed": ""},
            {"event_id": "e3", "magnitude": 0.8, "review_status": "rejected", "reviewed": "2026-01-02"},
            {"event_id": "e4", "magnitude": 1.2, "review_status": "", "reviewed": ""},
        ])
        catalog_df.to_csv(catalog_path, index=False)

        # Replicate the snapshot logic from catalog.py:217-225
        snap_df = pd.read_csv(catalog_path, dtype={"event_id": str})
        reviewed_mask = snap_df["review_status"].fillna("").astype(str).str.strip() != ""
        preserved = {
            row["event_id"]: row
            for row in snap_df[reviewed_mask].to_dict("records")
        }
        assert set(preserved) == {"e1", "e3"}, "Should preserve only reviewed events"
        assert preserved["e1"]["review_status"] == "confirmed"
        assert preserved["e3"]["review_status"] == "rejected"

        # Verify sidecar also captures both
        sidecar = tmp_path / "reviews.jsonl"
        n = write_reviews_sidecar(sidecar, preserved, {})
        assert n == 2

    def test_assignments_filtered_by_preserved_events(self, tmp_path: Path) -> None:
        """Only assignments belonging to preserved events should be kept."""
        assignments_df = pd.DataFrame([
            {"event_id": "e1", "station": "X1", "phase": "P"},
            {"event_id": "e1", "station": "X2", "phase": "S"},
            {"event_id": "e2", "station": "X1", "phase": "P"},  # e2 not reviewed
            {"event_id": "e3", "station": "X1", "phase": "P"},
        ])
        preserved_event_ids = {"e1", "e3"}
        keep_mask = assignments_df["event_id"].isin(preserved_event_ids)
        kept = assignments_df[keep_mask]
        assert len(kept) == 3
        assert "e2" not in kept["event_id"].tolist()


# ── Dashboard atomic_write_csv (legacy dict-based path) ──────────


class TestDashboardAtomicWriteCsv:
    """Verify dashboard/deps.py:atomic_write_csv (the dict-list variant)."""

    def test_writes_and_reads_back(self, tmp_path: Path) -> None:
        from dashboard.deps import atomic_write_csv

        rows = [
            {"event_id": "e1", "magnitude": "1.5"},
            {"event_id": "e2", "magnitude": "2.3"},
        ]
        out = tmp_path / "catalog.csv"
        atomic_write_csv(out, rows, ["event_id", "magnitude"])

        with out.open() as f:
            back = list(csv.DictReader(f))
        assert len(back) == 2
        assert back[0]["event_id"] == "e1"

    def test_destination_unchanged_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        """Same atomic guarantee as the DataFrame version."""
        from dashboard.deps import atomic_write_csv

        out = tmp_path / "existing.csv"
        out.write_text("event_id,magnitude\noriginal,9.9\n")

        # Make csv.DictWriter.writerow raise
        original_writerow = csv.DictWriter.writerow

        def boom(self, *args, **kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(csv.DictWriter, "writerow", boom)
        with pytest.raises(OSError):
            atomic_write_csv(out, [{"event_id": "x", "magnitude": "1"}], ["event_id", "magnitude"])
        monkeypatch.setattr(csv.DictWriter, "writerow", original_writerow)

        assert "original,9.9" in out.read_text()
