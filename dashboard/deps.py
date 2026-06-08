"""Shared paths, constants, and data loaders for the dashboard.

This module is import-side-effect-free — it just exposes paths and helpers
that every route module needs. Anything stateful (subprocess handles, cache
entries, WebSocket clients) lives in the module that owns it, not here.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # allow `from lib.* import ...`

from lib.constants import (  # noqa: E402  (path setup above)
    CONTINUOUS_LAG_HOURS,
    PIPELINE_START_DATE_ISO,
)

# ── Filesystem layout ────────────────────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGS_DIR = ROOT / "logs"
STATIONS_FILE = ROOT / "stations.json"

OUTPUT_DIR = ROOT / "output"
STATUS_FILE = OUTPUT_DIR / "pipeline_status.json"
CATALOG_FILE = OUTPUT_DIR / "5-catalog" / "catalog.csv"
ASSIGNMENTS_FILE = OUTPUT_DIR / "5-catalog" / "assignments.csv"
PICKS_DIR = OUTPUT_DIR / "3-picks"
EVENTS_DIR = OUTPUT_DIR / "4-events"
RAW_DIR = OUTPUT_DIR / "1-raw"
PROCESSED_DIR = OUTPUT_DIR / "2-processed"
FAULTS_FILE = STATIC_DIR / "faults.geojson"
QUARRIES_FILE = STATIC_DIR / "quarries.geojson"

PIPELINE_SCRIPT = ROOT / "run_pipeline.py"
PIPELINE_PID_FILE = OUTPUT_DIR / "pipeline.pid"

# ── Domain constants (mirror run_pipeline.py via lib.constants) ──
PIPELINE_START_DATE = date.fromisoformat(PIPELINE_START_DATE_ISO)
LAG_HOURS = CONTINUOUS_LAG_HOURS

VALID_STEP_NAMES = {"ingest", "process", "detect", "associate", "catalog"}

# ── Log parsing ──────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(ERROR|CRITICAL|WARNING)\s+(.+)$"
)
MAX_LOG_BYTES = 100 * 1024


# ── Cached stations loader ───────────────────────────────────────
_stations_cache: list | None = None


def load_stations() -> list:
    """Return the dashboard-shaped station list (cached after first call)."""
    global _stations_cache
    if _stations_cache is not None:
        return _stations_cache
    if not STATIONS_FILE.exists():
        _stations_cache = []
        return _stations_cache
    raw = json.loads(STATIONS_FILE.read_text())
    _stations_cache = [
        {
            "network": s["network"],
            "station": s["station"],
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "elevation_m": s["elevation_m"],
            "model": s.get("model", ""),
            "channels": s.get("channels", ""),
        }
        for s in raw
    ]
    return _stations_cache


# ── CSV readers ──────────────────────────────────────────────────
def read_catalog() -> list[dict]:
    """Read all rows of the master catalog as raw dicts (no parsing)."""
    if not CATALOG_FILE.exists():
        return []
    with open(CATALOG_FILE, newline="") as f:
        return list(csv.DictReader(f))


def read_assignments() -> list[dict]:
    """Read all rows of the master assignments file as raw dicts."""
    if not ASSIGNMENTS_FILE.exists():
        return []
    with open(ASSIGNMENTS_FILE, newline="") as f:
        return list(csv.DictReader(f))


def parse_catalog_row(r: dict) -> dict:
    """Coerce a raw catalog CSV row into typed JSON-ready dict."""
    return {
        "event_id": r.get("event_id", ""),
        "time": r.get("time", ""),
        "magnitude": float(r["magnitude"]) if r.get("magnitude") else None,
        "magnitude_type": r.get("magnitude_type", ""),
        "ml_err": float(r["ml_err"]) if r.get("ml_err") else None,
        "latitude": float(r["latitude"]) if r.get("latitude") else None,
        "longitude": float(r["longitude"]) if r.get("longitude") else None,
        "depth_km": float(r["depth_km"]) if r.get("depth_km") else None,
        "sigma_time": float(r["sigma_time"]) if r.get("sigma_time") else None,
        "sigma_amp": float(r["sigma_amp"]) if r.get("sigma_amp") else None,
        "num_picks": int(r["num_picks"]) if r.get("num_picks") else None,
        "num_ml_sta": int(r["num_ml_sta"]) if r.get("num_ml_sta") else None,
        "event_index": int(r["event_index"]) if r.get("event_index") else None,
        "reviewed": r.get("reviewed", ""),
        "review_status": r.get("review_status", ""),
        "event_type": r.get("event_type", "undetermined"),
    }


def filter_by_date(
    rows: list[dict], start_date: Optional[str], end_date: Optional[str]
) -> list[dict]:
    """Filter rows by date range on the 'time' field (YYYY-MM-DD prefix)."""
    if not start_date and not end_date:
        return rows
    out = []
    for r in rows:
        t = r.get("time", "")
        if len(t) < 10:
            continue
        day = t[:10]
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        out.append(r)
    return out


# ── Filesystem helpers ───────────────────────────────────────────
def dir_size(path: Path) -> int:
    """Total bytes in a directory tree (zero if missing/unreadable)."""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def file_mtime(path: Path) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ── Atomic CSV writer ────────────────────────────────────────────
def atomic_write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Write CSV via temp file + ``os.replace`` for crash safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_"
    )
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def strip_ansi(line: str) -> str:
    """Remove ANSI escape sequences from a log line."""
    return _ANSI_RE.sub("", line)
