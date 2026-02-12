"""Tests for lib/logger.py — logging setup and metrics."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.logger import LOG_FORMAT, MetricsWriter, setup_logging


class TestSetupLogging:
    def test_returns_logger(self, tmp_path):
        config = {
            "log_dir": str(tmp_path / "logs"),
            "log_max_bytes": 1048576,
            "log_backup_count": 1,
        }
        logger = setup_logging("test_logger", config)
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_logger"

    def test_creates_log_file(self, tmp_path):
        config = {
            "log_dir": str(tmp_path / "logs"),
            "log_max_bytes": 1048576,
            "log_backup_count": 1,
        }
        logger = setup_logging("test_file", config)
        logger.info("Test message")
        log_file = tmp_path / "logs" / "test_file.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "Test message" in content

    def test_debug_mode(self, tmp_path):
        config = {
            "log_dir": str(tmp_path / "logs"),
            "log_max_bytes": 1048576,
            "log_backup_count": 1,
        }
        logger = setup_logging("test_debug", config, debug=True)
        assert logger.level == logging.DEBUG

    def test_standardized_format(self, tmp_path):
        config = {
            "log_dir": str(tmp_path / "logs"),
            "log_max_bytes": 1048576,
            "log_backup_count": 1,
        }
        logger = setup_logging("test_fmt", config)
        logger.info("Hello world")
        log_file = tmp_path / "logs" / "test_fmt.log"
        content = log_file.read_text()
        # Check standardized format: [INFO] test_fmt |
        assert "[INFO]" in content
        assert "test_fmt" in content
        assert "|" in content


class TestMetricsWriter:
    def test_writes_jsonl(self, tmp_path):
        path = tmp_path / "metrics.jsonl"
        writer = MetricsWriter(path)
        writer.record("ingest", station="AM.R0F2D", duration_s=12.3)
        writer.record("process", station="AM.R4B41", duration_s=5.1)

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["step"] == "ingest"
        assert entry1["station"] == "AM.R0F2D"
        assert entry1["duration_s"] == 12.3
        assert "timestamp" in entry1

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "metrics.jsonl"
        writer = MetricsWriter(path)
        writer.record("test", key="value")
        assert path.exists()
