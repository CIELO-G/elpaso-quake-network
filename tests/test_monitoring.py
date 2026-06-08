"""Tests for lib/monitoring.py — health checks and alerting."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import monitoring

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_catalog(path: Path, rows: list[dict]) -> None:
    """Write a minimal catalog CSV for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["event_id", "time", "magnitude", "latitude", "longitude", "depth_km"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# check_disk_space
# ---------------------------------------------------------------------------


class TestCheckDiskSpace:
    def test_returns_ok_when_below_threshold(self):
        ok, msg = monitoring.check_disk_space(threshold_percent=99.9)
        assert ok is True
        assert "OK" in msg

    def test_returns_not_ok_when_above_threshold(self):
        ok, msg = monitoring.check_disk_space(threshold_percent=0.1)
        assert ok is False
        assert "%" in msg

    def test_message_contains_free_gb(self):
        _, msg = monitoring.check_disk_space()
        assert "GB free" in msg


# ---------------------------------------------------------------------------
# check_recent_events
# ---------------------------------------------------------------------------


class TestCheckRecentEvents:
    def test_no_catalog_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monitoring, "CATALOG_FILE", tmp_path / "nonexistent.csv")
        ok, msg = monitoring.check_recent_events()
        assert ok is True
        assert "No catalog file" in msg

    def test_empty_catalog(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        _write_catalog(cat, [])
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        ok, msg = monitoring.check_recent_events()
        assert ok is True
        assert "No events" in msg

    def test_recent_event_is_ok(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": recent,
                    "magnitude": "2.0",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "5",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        ok, msg = monitoring.check_recent_events(hours=48)
        assert ok is True
        assert "Latest event" in msg

    def test_old_event_triggers_alert(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": old,
                    "magnitude": "2.0",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "5",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        ok, msg = monitoring.check_recent_events(hours=48)
        assert ok is False
        assert "No new events" in msg

    def test_malformed_time_skipped(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": "not-a-date",
                    "magnitude": "2.0",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "5",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        ok, msg = monitoring.check_recent_events()
        assert ok is True  # no parseable events → treated as "no events yet"


# ---------------------------------------------------------------------------
# check_significant_events
# ---------------------------------------------------------------------------


class TestCheckSignificantEvents:
    def test_no_catalog_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monitoring, "CATALOG_FILE", tmp_path / "nope.csv")
        result = monitoring.check_significant_events()
        assert result == []

    def test_finds_large_event(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": recent,
                    "magnitude": "4.5",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "10",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        result = monitoring.check_significant_events(magnitude_threshold=4.0)
        assert len(result) == 1
        assert result[0]["magnitude"] == 4.5

    def test_ignores_small_events(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": recent,
                    "magnitude": "2.0",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "5",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        result = monitoring.check_significant_events(magnitude_threshold=4.0)
        assert result == []

    def test_ignores_old_large_events(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": old,
                    "magnitude": "5.0",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "10",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        result = monitoring.check_significant_events(magnitude_threshold=4.0)
        assert result == []


# ---------------------------------------------------------------------------
# send_webhook
# ---------------------------------------------------------------------------


class TestSendWebhook:
    def test_skips_when_no_url(self, monkeypatch):
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        # Should not raise
        monitoring.send_webhook("test", "body")

    def test_sends_payload(self, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://example.com/hook")
        with mock.patch("lib.monitoring.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock()
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)
            monitoring.send_webhook("Test Subject", "Test Body")
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data)
            assert "Test Subject" in payload["text"]
            assert "Test Body" in payload["text"]

    def test_handles_network_error(self, monkeypatch, capsys):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://example.com/hook")
        with mock.patch(
            "lib.monitoring.urllib.request.urlopen", side_effect=OSError("connection refused")
        ):
            monitoring.send_webhook("subj", "body")  # should not raise
        captured = capsys.readouterr()
        assert "WARNING" in captured.out


# ---------------------------------------------------------------------------
# run_monitoring_checks
# ---------------------------------------------------------------------------


class TestRunMonitoringChecks:
    def test_returns_dict_with_expected_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monitoring, "CATALOG_FILE", tmp_path / "cat.csv")
        results = monitoring.run_monitoring_checks()
        assert "disk_space" in results
        assert "recent_events" in results
        assert "significant_events" in results

    def test_calls_alert_fn_on_disk_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monitoring, "CATALOG_FILE", tmp_path / "cat.csv")
        # Patch check_disk_space to always fail
        monkeypatch.setattr(monitoring, "check_disk_space", lambda: (False, "disk full"))
        monkeypatch.setattr(monitoring, "send_webhook", lambda s, b: None)
        alerts = []
        monitoring.run_monitoring_checks(send_alert_fn=lambda s, b: alerts.append(s))
        assert any("Disk" in a for a in alerts)

    def test_calls_alert_fn_on_significant_event(self, tmp_path, monkeypatch):
        cat = tmp_path / "catalog.csv"
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_catalog(
            cat,
            [
                {
                    "event_id": "ep1",
                    "time": recent,
                    "magnitude": "5.0",
                    "latitude": "31.8",
                    "longitude": "-106.4",
                    "depth_km": "10",
                }
            ],
        )
        monkeypatch.setattr(monitoring, "CATALOG_FILE", cat)
        monkeypatch.setattr(monitoring, "send_webhook", lambda s, b: None)
        alerts = []
        monitoring.run_monitoring_checks(send_alert_fn=lambda s, b: alerts.append(s))
        assert any("M5.0" in a for a in alerts)
