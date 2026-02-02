"""Shared SQLite download/processing tracker for the El Paso seismic pipeline."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class DownloadDB:
    """Tracks which waveform chunks have been fetched."""

    def __init__(self, base_dir):
        db_path = Path(base_dir) / "1-downloads.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._create_table()

    def _create_table(self):
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

    def is_downloaded(self, network, station, location, channel,
                      start_time, end_time):
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM downloads
                   WHERE network=? AND station=? AND location=? AND channel=?
                     AND start_time=? AND end_time=? AND status='success'""",
                (network, station, location, channel,
                 str(start_time), str(end_time)),
            ).fetchone()
            return row is not None

    def record(self, network, station, location, channel,
               start_time, end_time, status, filepaths=None, error=None):
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

    def close(self):
        self.conn.close()
