"""Tests for lib/db.py — SQLite download tracker."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DownloadDB


class TestDownloadDB:
    def test_create_and_record(self, tmp_path):
        db = DownloadDB(tmp_path)
        db.record(
            "AM",
            "R0F2D",
            "00",
            "EHZ",
            "2026-01-01T00:00:00",
            "2026-01-01T01:00:00",
            "success",
            filepaths=["/tmp/test.mseed"],
        )
        assert db.is_downloaded(
            "AM", "R0F2D", "00", "EHZ", "2026-01-01T00:00:00", "2026-01-01T01:00:00"
        )
        db.close()

    def test_not_downloaded(self, tmp_path):
        db = DownloadDB(tmp_path)
        assert not db.is_downloaded(
            "AM", "R0F2D", "00", "EHZ", "2026-01-01T00:00:00", "2026-01-01T01:00:00"
        )
        db.close()

    def test_failed_not_treated_as_downloaded(self, tmp_path):
        db = DownloadDB(tmp_path)
        db.record(
            "AM",
            "R0F2D",
            "00",
            "EHZ",
            "2026-01-01T00:00:00",
            "2026-01-01T01:00:00",
            "failed",
            error="503 Service Unavailable",
        )
        assert not db.is_downloaded(
            "AM", "R0F2D", "00", "EHZ", "2026-01-01T00:00:00", "2026-01-01T01:00:00"
        )
        db.close()

    def test_replace_failed_with_success(self, tmp_path):
        db = DownloadDB(tmp_path)
        db.record(
            "AM",
            "R0F2D",
            "00",
            "EHZ",
            "2026-01-01T00:00:00",
            "2026-01-01T01:00:00",
            "failed",
            error="timeout",
        )
        assert not db.is_downloaded(
            "AM", "R0F2D", "00", "EHZ", "2026-01-01T00:00:00", "2026-01-01T01:00:00"
        )
        db.record(
            "AM", "R0F2D", "00", "EHZ", "2026-01-01T00:00:00", "2026-01-01T01:00:00", "success"
        )
        assert db.is_downloaded(
            "AM", "R0F2D", "00", "EHZ", "2026-01-01T00:00:00", "2026-01-01T01:00:00"
        )
        db.close()

    def test_db_file_created(self, tmp_path):
        db = DownloadDB(tmp_path)
        assert (tmp_path / "1-downloads.db").exists()
        db.close()
