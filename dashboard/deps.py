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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # allow `from lib.* import ...`

from lib.constants import (  # noqa: E402  (path setup above)
    CONTINUOUS_LAG_HOURS,
    PIPELINE_START_DATE_ISO,
)
from lib.models import channel_layout  # noqa: E402

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
# Cached per stations.json mtime (B7): editing the file — adding a station,
# retiring one — shows up on the next request, no app restart needed.
_stations_cache: list | None = None
_stations_cache_mtime: float | None = None


def load_stations() -> list:
    """Return the dashboard-shaped station list, reloaded when stations.json changes."""
    global _stations_cache, _stations_cache_mtime
    if not STATIONS_FILE.exists():
        _stations_cache, _stations_cache_mtime = [], None
        return _stations_cache
    mtime = STATIONS_FILE.stat().st_mtime
    if _stations_cache is not None and mtime == _stations_cache_mtime:
        return _stations_cache
    raw = json.loads(STATIONS_FILE.read_text())
    _stations_cache_mtime = mtime
    _stations_cache = [
        {
            "network": s["network"],
            "station": s["station"],
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "elevation_m": s["elevation_m"],
            "model": s.get("model", ""),
            "channels": s.get("channels", ""),
            "start_date": s.get("start_date", ""),
            "end_date": s.get("end_date", ""),
            # Channel codes this station actually records (B7): e.g. HHE/HHN/HHZ
            # for broadband, EHZ alone for a vertical-only Shake. Replaces the
            # hard-coded EHZ/EHN/EHE list in the 3C waveform fan-out.
            "channel_codes": _channel_codes(s),
        }
        for s in raw
    ]
    return _stations_cache


def _channel_codes(entry: dict) -> list[str]:
    try:
        band, comps = channel_layout(entry)
    except Exception:  # noqa: BLE001 - never let a bad entry break the dashboard
        return ["EHZ"]
    return [f"{band}{c}" for c in comps]


def station_channels(station: dict, requested: str) -> list[str]:
    """Channels to load for ``station`` given a request of '3C' or one code.

    '3C' expands to the station's own codes (Z first, as the plots expect
    Z/N/E order). A single code is kept as is; the file lookup already
    falls back across bands (EHZ -> HHZ).
    """
    if requested == "3C":
        codes = list(station.get("channel_codes") or ["EHZ"])
        order = {"Z": 0, "N": 1, "E": 2, "1": 1, "2": 2}
        return sorted(codes, key=lambda c: order.get(c[-1], 9))
    return [requested]


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


def _as_int(v) -> int | None:
    """Safely coerce '1', '1.0', 1, 1.0 → 1; '' or None → None.

    Pandas can promote int columns to float when any NaN appears in the
    column during a concat/round-trip; values then end up as '1.0' strings
    in the CSV. int('1.0') raises ValueError, so go through float() first.
    """
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


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
        "num_picks": _as_int(r.get("num_picks")),
        "num_ml_sta": _as_int(r.get("num_ml_sta")),
        "event_index": _as_int(r.get("event_index")),
        "reviewed": r.get("reviewed", ""),
        "review_status": r.get("review_status", ""),
        "event_type": r.get("event_type", "undetermined"),
        "triage": r.get("triage", "") or "",
        "triage_reason": r.get("triage_reason", "") or "",
    }


def filter_by_date(rows: list[dict], start_date: str | None, end_date: str | None) -> list[dict]:
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
    """Write CSV via temp file + fsync + ``os.replace`` for crash safety.

    fsync is what guarantees durability across power loss / OS crash;
    ``os.replace`` alone only guarantees atomic visibility of whatever
    happens to be in the page cache at rename time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            f.flush()
            os.fsync(f.fileno())
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
