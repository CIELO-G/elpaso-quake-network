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
        self._migrate()

    SCHEMA_VERSION = 1

    def _migrate(self) -> None:
        """Bring an existing database up to SCHEMA_VERSION (idempotent).

        v1 (2026-10-03): ``coverage`` REAL — fraction of the requested
        samples actually returned for a ``success`` chunk. NULL on rows
        written before v1; those are treated as complete so a migration
        never triggers a re-download of the whole history.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(downloads)")}
        if "coverage" not in cols:
            self.conn.execute("ALTER TABLE downloads ADD COLUMN coverage REAL")
        self.conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        self.conn.commit()

    def chunk_status(
        self,
        network: str,
        station: str,
        location: str,
        channel: str,
        start_time: Any,
        end_time: Any,
    ) -> tuple[str, float | None] | None:
        """``(status, coverage)`` of the recorded attempt, or None if never tried."""
        with self._lock:
            row = self.conn.execute(
                """SELECT status, coverage FROM downloads
                   WHERE network=? AND station=? AND location=? AND channel=?
                     AND start_time=? AND end_time=?""",
                (network, station, location, channel, str(start_time), str(end_time)),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def needs_fetch(
        self,
        network: str,
        station: str,
        location: str,
        channel: str,
        start_time: Any,
        end_time: Any,
        *,
        min_coverage: float = 0.0,
        within_refetch_window: bool = True,
        force: bool = False,
    ) -> bool:
        """Decide whether a chunk should be (re)requested.

        - never tried, or last attempt ``failed``: fetch.
        - ``success`` with coverage >= ``min_coverage`` (or unknown/legacy): skip.
        - ``success`` but short, or ``no_data``: fetch while the chunk is still
          inside the re-fetch window (late uploads), or when ``force`` is set;
          otherwise accept what we have.
        """
        rec = self.chunk_status(network, station, location, channel, start_time, end_time)
        if rec is None:
            return True
        status, coverage = rec
        if status == "failed":
            return True
        if status == "success" and (coverage is None or coverage >= min_coverage):
            return False
        # short success or no_data
        return force or within_refetch_window

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
                (network, station, location, channel, str(start_time), str(end_time)),
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
        coverage: float | None = None,
    ) -> None:
        """Record a download attempt result (``coverage`` only for ``success``)."""
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO downloads
                   (network, station, location, channel, start_time, end_time,
                    status, filepaths, download_time, error_message, coverage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    network,
                    station,
                    location,
                    channel,
                    str(start_time),
                    str(end_time),
                    status,
                    json.dumps(filepaths) if filepaths else None,
                    datetime.now(timezone.utc).isoformat(),
                    error,
                    coverage,
                ),
            )
            self.conn.commit()

    def short_chunks(self, min_coverage: float, since_end_time: str | None = None) -> list[tuple]:
        """Rows recorded as success with coverage below ``min_coverage``.

        Returns ``(network, station, start_time, end_time, coverage)`` tuples,
        optionally only for chunks ending at or after ``since_end_time``.
        """
        q = """SELECT network, station, start_time, end_time, coverage FROM downloads
               WHERE status='success' AND coverage IS NOT NULL AND coverage < ?"""
        params: list = [min_coverage]
        if since_end_time:
            q += " AND end_time >= ?"
            params.append(since_end_time)
        with self._lock:
            return self.conn.execute(q + " ORDER BY start_time", params).fetchall()

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
