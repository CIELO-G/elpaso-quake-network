"""Shared SQLite download/processing tracker for the El Paso seismic pipeline."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DownloadDB:
    """Tracks which waveform chunks have been fetched.

    Thread-safe via internal lock. Uses WAL journaling for performance.
    """

    def __init__(self, base_dir: str | Path) -> None:
        db_path = Path(base_dir) / "1-downloads.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._create_table()

    def _create_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                network         TEXT NOT NULL,
                station         TEXT NOT NULL,
                location        TEXT NOT NULL,
                channel         TEXT NOT NULL,
                start_time      TEXT NOT NULL,
                end_time        TEXT NOT NULL,
                status          TEXT NOT NULL,
                filepaths       TEXT,
                download_time   TEXT NOT NULL,
                error_message   TEXT,
                UNIQUE(network, station, location, channel, start_time, end_time)
            )
            """
        )
        self.conn.commit()

    def is_downloaded(
        self,
        network: str,
        station: str,
        location: str,
        channel: str,
        start_time: Any,
        end_time: Any,
    ) -> bool:
        """Check if a chunk has already been successfully downloaded."""
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM downloads
                   WHERE network=? AND station=? AND location=? AND channel=?
                     AND start_time=? AND end_time=? AND status='success'""",
                (network, station, location, channel,
                 str(start_time), str(end_time)),
            ).fetchone()
            return row is not None

    def record(
        self,
        network: str,
        station: str,
        location: str,
        channel: str,
        start_time: Any,
        end_time: Any,
        status: str,
        filepaths: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        """Record a download attempt result."""
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO downloads
                   (network, station, location, channel, start_time, end_time,
                    status, filepaths, download_time, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (network, station, location, channel,
                 str(start_time), str(end_time), status,
                 json.dumps(filepaths) if filepaths else None,
                 datetime.now(timezone.utc).isoformat(), error),
            )
            self.conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
