"""Shared logging setup for the El Paso seismic pipeline."""

from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Standardised log format: timestamp [LEVEL] logger_name | message
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    name: str,
    config: dict,
    debug: bool = False,
) -> logging.Logger:
    """Set up console + rotating-file logging.

    Parameters
    ----------
    name : str
        Logger name (also used as the log filename stem).
    config : dict
        Must contain ``log_dir``, ``log_max_bytes``, ``log_backup_count``.
    debug : bool
        If True, set level to DEBUG; otherwise INFO.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    # Console
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Rotating file
    log_dir = Path(config["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=config["log_max_bytes"],
        backupCount=config["log_backup_count"],
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


class MetricsWriter:
    """Append structured metrics as JSON lines to a rotating metrics file.

    Usage::

        metrics = MetricsWriter("logs/metrics.jsonl")
        metrics.record("ingest", station="AM.R0F2D", duration_s=12.3, chunks=24)
    """

    MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
    BACKUP_COUNT = 5  # keep 5 rotated files

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=self.MAX_BYTES,
            backupCount=self.BACKUP_COUNT,
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger(f"metrics.{self.path.stem}")
        self._logger.handlers.clear()
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def record(self, step: str, **kwargs) -> None:
        """Write one metric line with timestamp, step, and arbitrary fields."""
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "step": step,
            **kwargs,
        }
        self._logger.info(json.dumps(entry, default=str))
