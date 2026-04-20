"""FastAPI application — serves dashboard UI and pipeline status API."""

import csv
import hashlib
import json
import math
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # allow imports from project root (lib.*)
STATUS_FILE = ROOT / "output" / "pipeline_status.json"
LOGS_DIR = ROOT / "logs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIONS_FILE = ROOT / "stations.json"
CATALOG_FILE = ROOT / "output" / "5-catalog" / "catalog.csv"
PICKS_DIR = ROOT / "output" / "3-picks"
EVENTS_DIR = ROOT / "output" / "4-events"
RAW_DIR = ROOT / "output" / "1-raw"
OUTPUT_DIR = ROOT / "output"

PROCESSED_DIR = ROOT / "output" / "2-processed"

# ── Pipeline control ─────────────────────────────────────────────
PIPELINE_SCRIPT = ROOT / "run_pipeline.py"
PIPELINE_PID_FILE = ROOT / "output" / "pipeline.pid"
_pipeline_proc: subprocess.Popen | None = None

VALID_STEP_NAMES = {"ingest", "process", "detect", "associate", "catalog"}

PIPELINE_START_DATE = date(2025, 11, 1)
LAG_HOURS = 6

# ANSI escape sequence pattern
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Max log response size (100 KB)
MAX_LOG_BYTES = 100 * 1024

# ── Authentication ────────────────────────────────────────────────
AUTH_ENABLED = os.environ.get("DASHBOARD_AUTH_ENABLED", "").lower() in ("1", "true", "yes")
AUTH_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

_app_start_time = time.monotonic()

app = FastAPI(title="El Paso Seismic Monitor")

# ── CORS ──────────────────────────────────────────────────────────
_CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Basic Auth Middleware ─────────────────────────────────────────
class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        # Allow health endpoint without auth
        if request.url.path in ("/api/health", "/api/v1/health"):
            return await call_next(request)
        # Allow WebSocket upgrade (auth checked in WS handler)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        import base64
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if secrets.compare_digest(user, AUTH_USERNAME) and secrets.compare_digest(passwd, AUTH_PASSWORD):
                    return await call_next(request)
            except Exception:
                pass
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Dashboard"'},
        )


app.add_middleware(BasicAuthMiddleware)


# ── Rate Limiting (in-memory, per IP) ────────────────────────────
_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 60  # requests per minute
RATE_WINDOW = 60.0  # seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        # Clean old entries
        entries = _rate_store[client_ip]
        cutoff = now - RATE_WINDOW
        _rate_store[client_ip] = [t for t in entries if t > cutoff]
        if len(_rate_store[client_ip]) >= RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Max 60 requests per minute."},
            )
        _rate_store[client_ip].append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)


# ── Caching with TTL and file-mtime invalidation ─────────────────
class _CacheEntry:
    __slots__ = ("data", "etag", "created_at", "file_mtime")

    def __init__(self, data, etag: str, file_mtime: Optional[float]):
        self.data = data
        self.etag = etag
        self.created_at = time.monotonic()
        self.file_mtime = file_mtime


_cache: dict[str, _CacheEntry] = {}


def _file_mtime(path: Path) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _get_cached(key: str, ttl: float, watch_file: Optional[Path] = None):
    entry = _cache.get(key)
    if entry is None:
        return None
    # TTL check
    if (time.monotonic() - entry.created_at) > ttl:
        return None
    # File mtime invalidation
    if watch_file is not None:
        current_mtime = _file_mtime(watch_file)
        if current_mtime != entry.file_mtime:
            return None
    return entry


def _set_cached(key: str, data, watch_file: Optional[Path] = None):
    mtime = _file_mtime(watch_file) if watch_file else None
    etag_raw = json.dumps(data, sort_keys=True, default=str)
    etag = hashlib.md5(etag_raw.encode()).hexdigest()
    _cache[key] = _CacheEntry(data, etag, mtime)
    return etag


def _make_etag_response(data, etag: str, request: Request):
    if_none_match = request.headers.get("if-none-match", "")
    if if_none_match == etag:
        from starlette.responses import Response
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, headers={"ETag": etag})


# ── Stations cache (loaded once) ─────────────────────────────────
_stations_cache: list | None = None


def _load_stations() -> list:
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


def _read_catalog() -> list[dict]:
    if not CATALOG_FILE.exists():
        return []
    with open(CATALOG_FILE, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _dir_size(path: Path) -> int:
    """Total bytes in a directory tree."""
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


ASSIGNMENTS_FILE = ROOT / "output" / "5-catalog" / "assignments.csv"


def _parse_catalog_row(r: dict) -> dict:
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


def _read_assignments() -> list[dict]:
    if not ASSIGNMENTS_FILE.exists():
        return []
    with open(ASSIGNMENTS_FILE, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _filter_by_date(rows: list[dict], start_date: Optional[str], end_date: Optional[str]) -> list[dict]:
    """Filter rows by date range on the 'time' field."""
    if not start_date and not end_date:
        return rows
    filtered = []
    for r in rows:
        t = r.get("time", "")
        if len(t) < 10:
            continue
        day = t[:10]
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        filtered.append(r)
    return filtered


# ── WebSocket status push ─────────────────────────────────────────
_ws_clients: set[WebSocket] = set()


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        # Send initial status
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                await websocket.send_json(data)
            except (json.JSONDecodeError, OSError):
                pass
        # Keep alive — client can send pings, we watch for disconnect
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        _ws_clients.discard(websocket)


async def _broadcast_status():
    """Push current status to all connected WebSocket clients."""
    if not _ws_clients or not STATUS_FILE.exists():
        return
    try:
        data = json.loads(STATUS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ── Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


FAULTS_FILE = STATIC_DIR / "faults.geojson"


@app.get("/api/faults")
async def faults():
    if not FAULTS_FILE.exists():
        return JSONResponse({"type": "FeatureCollection", "features": []})
    return FileResponse(FAULTS_FILE, media_type="application/geo+json")


@app.get("/api/health")
@app.get("/api/v1/health")
async def health():
    """Structured health check endpoint."""
    pipeline_status = "unknown"
    last_run_time = None
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
            pipeline_status = data.get("pipeline", {}).get("status", "unknown")
            last_run_time = data.get("pipeline", {}).get("started_at")
        except (json.JSONDecodeError, OSError):
            pass

    catalog_size = 0
    if CATALOG_FILE.exists():
        try:
            with open(CATALOG_FILE, newline="") as f:
                catalog_size = sum(1 for _ in f) - 1  # minus header
                if catalog_size < 0:
                    catalog_size = 0
        except OSError:
            pass

    usage = shutil.disk_usage(ROOT)
    uptime_sec = round(time.monotonic() - _app_start_time, 1)

    return {
        "status": "healthy",
        "pipeline_status": pipeline_status,
        "last_run_time": last_run_time,
        "catalog_size": catalog_size,
        "disk_usage_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        "uptime_seconds": uptime_sec,
    }


@app.get("/api/status")
@app.get("/api/v1/status")
async def api_status(request: Request):
    entry = _get_cached("status", ttl=2.0, watch_file=STATUS_FILE)
    if entry:
        return _make_etag_response(entry.data, entry.etag, request)
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            raise HTTPException(status_code=500, detail="Failed to read pipeline status file")
    else:
        data = {"pipeline": {"status": "idle"}, "steps": []}
    etag = _set_cached("status", data, watch_file=STATUS_FILE)
    # Broadcast to WebSocket clients
    await _broadcast_status()
    return _make_etag_response(data, etag, request)


@app.get("/api/logs/{step_name}", response_class=PlainTextResponse)
@app.get("/api/v1/logs/{step_name}", response_class=PlainTextResponse)
async def logs(step_name: str, lines: int = Query(default=100, ge=1, le=5000)):
    if step_name not in VALID_STEP_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown step: {step_name}. Valid: {', '.join(sorted(VALID_STEP_NAMES))}")
    log_path = LOGS_DIR / f"{step_name}.log"
    if not log_path.exists():
        return PlainTextResponse(f"No log file for {step_name}")
    try:
        all_lines = log_path.read_text().splitlines()
    except OSError:
        raise HTTPException(status_code=500, detail=f"Failed to read log file for {step_name}")
    tail = all_lines[-lines:]
    # Strip ANSI codes
    cleaned = [_ANSI_RE.sub("", line) for line in tail]
    output = "\n".join(cleaned) + "\n"
    # Limit response size
    if len(output.encode("utf-8")) > MAX_LOG_BYTES:
        output_bytes = output.encode("utf-8")[:MAX_LOG_BYTES]
        output = output_bytes.decode("utf-8", errors="ignore") + "\n... [truncated to 100KB]\n"
    return output


@app.get("/api/stations")
@app.get("/api/v1/stations")
async def stations():
    return _load_stations()


@app.get("/api/catalog")
@app.get("/api/v1/catalog")
async def catalog(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    sort_by: Optional[str] = Query(default=None, pattern=r"^(time|magnitude|depth_km|num_picks)$"),
    sort_order: Optional[str] = Query(default="desc", pattern=r"^(asc|desc)$"),
    min_magnitude: Optional[float] = Query(default=None),
    max_depth: Optional[float] = Query(default=None),
    min_picks: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None, min_length=1, max_length=100),
    review_status: Optional[str] = Query(default=None, pattern=r"^(unreviewed|confirmed|rejected)$"),
):
    entry = _get_cached("catalog", ttl=30.0, watch_file=CATALOG_FILE)
    if entry:
        all_events = entry.data
    else:
        rows = _read_catalog()
        all_events = [_parse_catalog_row(r) for r in rows]
        _set_cached("catalog", all_events, watch_file=CATALOG_FILE)

    # Date filtering
    if start_date or end_date:
        all_events = _filter_by_date(all_events, start_date, end_date)

    # Science filters
    if min_magnitude is not None:
        all_events = [e for e in all_events if e["magnitude"] is not None and e["magnitude"] >= min_magnitude]
    if max_depth is not None:
        all_events = [e for e in all_events if e["depth_km"] is not None and e["depth_km"] <= max_depth]
    if min_picks is not None:
        all_events = [e for e in all_events if e["num_picks"] is not None and e["num_picks"] >= min_picks]
    if search:
        search_lower = search.lower()
        all_events = [e for e in all_events if search_lower in (e.get("event_id") or "").lower()
                      or search_lower in (e.get("time") or "").lower()]
    if review_status:
        if review_status == "unreviewed":
            all_events = [e for e in all_events if not e.get("review_status")]
        else:
            all_events = [e for e in all_events if e.get("review_status") == review_status]

    # Sorting
    if sort_by:
        reverse = sort_order == "desc"
        all_events = sorted(
            all_events,
            key=lambda e: (e[sort_by] is None, e[sort_by] if e[sort_by] is not None else 0),
            reverse=reverse,
        )

    total = len(all_events)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_events = all_events[start_idx:end_idx]

    return {
        "events": page_events,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


@app.get("/api/stats")
@app.get("/api/v1/stats")
async def stats(request: Request):
    entry = _get_cached("stats", ttl=30.0, watch_file=CATALOG_FILE)
    if entry:
        return _make_etag_response(entry.data, entry.etag, request)

    rows = _read_catalog()
    catalog_events = len(rows)
    magnitudes = [float(r["magnitude"]) for r in rows if r.get("magnitude")]
    magnitude_min = min(magnitudes) if magnitudes else None
    magnitude_max = max(magnitudes) if magnitudes else None

    latest_event_time = None
    latest_event_id = None
    latest_event_lat = None
    latest_event_lon = None
    reviewed_count = sum(1 for r in rows if r.get("review_status"))
    if rows:
        latest = rows[-1]
        latest_event_time = latest.get("time")
        latest_event_id = latest.get("event_id")
        latest_event_lat = float(latest["latitude"]) if latest.get("latitude") else None
        latest_event_lon = float(latest["longitude"]) if latest.get("longitude") else None

    # Count picks
    total_picks = 0
    days_with_picks = 0
    pick_files = list(PICKS_DIR.rglob("*.picks.csv"))
    for pf in pick_files:
        try:
            with open(pf) as fh:
                count = sum(1 for _ in fh) - 1
            total_picks += count
            if count > 0:
                days_with_picks += 1
        except OSError:
            pass

    # Count event day files
    days_with_events = 0
    for ef in EVENTS_DIR.rglob("*.events.csv"):
        try:
            with open(ef) as fh:
                if sum(1 for _ in fh) > 1:
                    days_with_events += 1
        except OSError:
            pass

    # Count raw day directories
    days_with_raw = 0
    if RAW_DIR.exists():
        for year_dir in RAW_DIR.iterdir():
            if year_dir.is_dir():
                for d in year_dir.iterdir():
                    if d.is_dir() and sum(1 for _ in d.glob("*.mseed")) >= 3:
                        days_with_raw += 1

    station_count = len(_load_stations())

    data = {
        "catalog_events": catalog_events,
        "magnitude_min": magnitude_min,
        "magnitude_max": magnitude_max,
        "total_picks": total_picks,
        "days_with_picks": days_with_picks,
        "days_with_events": days_with_events,
        "days_with_raw": days_with_raw,
        "station_count": station_count,
        "latest_event_time": latest_event_time,
        "latest_event_id": latest_event_id,
        "latest_event_lat": latest_event_lat,
        "latest_event_lon": latest_event_lon,
        "reviewed_count": reviewed_count,
    }
    etag = _set_cached("stats", data, watch_file=CATALOG_FILE)
    return _make_etag_response(data, etag, request)


@app.get("/api/disk")
@app.get("/api/v1/disk")
async def disk(request: Request):
    entry = _get_cached("disk", ttl=60.0)
    if entry:
        return _make_etag_response(entry.data, entry.etag, request)

    usage = shutil.disk_usage(ROOT)
    total_gb = round(usage.total / (1024 ** 3), 1)
    used_gb = round(usage.used / (1024 ** 3), 1)
    free_gb = round(usage.free / (1024 ** 3), 1)
    usage_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0

    step_dirs = ["1-raw", "1-metadata", "2-processed", "3-picks", "4-events", "5-catalog"]
    breakdown = {}
    output_total = 0
    for d in step_dirs:
        size = _dir_size(OUTPUT_DIR / d)
        breakdown[d] = round(size / (1024 ** 3), 3)
        output_total += size

    db_file = OUTPUT_DIR / "1-downloads.db"
    if db_file.exists():
        db_size = db_file.stat().st_size
        breakdown["1-downloads.db"] = round(db_size / (1024 ** 3), 3)
        output_total += db_size

    data = {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "usage_percent": usage_percent,
        "output_size_gb": round(output_total / (1024 ** 3), 2),
        "breakdown": breakdown,
    }
    etag = _set_cached("disk", data)
    return _make_etag_response(data, etag, request)


def _count_day_dirs(base: Path, min_files: int = 0) -> int:
    count = 0
    if not base.exists():
        return 0
    for year_dir in base.iterdir():
        if year_dir.is_dir() and year_dir.name.isdigit():
            for d in year_dir.iterdir():
                if not d.is_dir():
                    continue
                if min_files > 0:
                    if sum(1 for f in d.iterdir() if f.is_file()) >= min_files:
                        count += 1
                else:
                    count += 1
    return count


def _latest_day_dir(base: Path, min_files: int = 0) -> str | None:
    """Return the latest YYYY-MM-DD date string from year/jday dirs."""
    latest = None
    if not base.exists():
        return None
    for year_dir in base.iterdir():
        if year_dir.is_dir() and year_dir.name.isdigit():
            year = int(year_dir.name)
            for d in year_dir.iterdir():
                if not d.is_dir() or not d.name.isdigit():
                    continue
                if min_files > 0 and sum(1 for f in d.iterdir() if f.is_file()) < min_files:
                    continue
                jday = int(d.name)
                dt = datetime(year, 1, 1) + timedelta(days=jday - 1)
                ds = dt.strftime("%Y-%m-%d")
                if latest is None or ds > latest:
                    latest = ds
    return latest


def _latest_day_csv(base: Path, glob_pattern: str) -> str | None:
    """Return the latest YYYY-MM-DD date from year/jday CSV files."""
    latest = None
    for csv_file in base.rglob(glob_pattern):
        parts = csv_file.parts
        try:
            idx = next(i for i, p in enumerate(parts) if p.isdigit() and len(p) == 4)
            year = int(parts[idx])
            jday = int(parts[idx + 1])
            dt = datetime(year, 1, 1) + timedelta(days=jday - 1)
            ds = dt.strftime("%Y-%m-%d")
            if latest is None or ds > latest:
                latest = ds
        except (StopIteration, ValueError, IndexError):
            pass
    return latest


def _count_days_with_data(base: Path, glob_pattern: str, require_rows: bool = False) -> int:
    count = 0
    for csv_file in base.rglob(glob_pattern):
        try:
            if require_rows:
                with open(csv_file) as fh:
                    if sum(1 for _ in fh) > 1:
                        count += 1
            else:
                count += 1
        except OSError:
            pass
    return count


@app.get("/api/progress")
@app.get("/api/v1/progress")
async def progress():
    target_date = (
        datetime.now(timezone.utc) - timedelta(hours=LAG_HOURS)
    ).date()
    total_days = max((target_date - PIPELINE_START_DATE).days + 1, 0)

    days_ingested = _count_day_dirs(RAW_DIR, min_files=3)
    days_processed = _count_day_dirs(PROCESSED_DIR)
    days_detected = _count_days_with_data(PICKS_DIR, "*.picks.csv")
    days_associated = _count_days_with_data(EVENTS_DIR, "*.events.csv")

    last_ingested = _latest_day_dir(RAW_DIR, min_files=3)
    last_processed = _latest_day_dir(PROCESSED_DIR)
    last_detected = _latest_day_csv(PICKS_DIR, "*.picks.csv")
    last_associated = _latest_day_csv(EVENTS_DIR, "*.events.csv")

    current_day = None
    pipeline_status = None
    mode = "single"
    waiting = False
    next_run_at = None
    days_completed_cont = None
    days_skipped = []
    last_completed_at = None

    if STATUS_FILE.exists():
        try:
            status_data = json.loads(STATUS_FILE.read_text())
            pipeline_status = status_data.get("pipeline", {}).get("status")
            mode = status_data.get("pipeline", {}).get("mode", "single")
            args = status_data.get("pipeline", {}).get("args", {})
            current_day = args.get("start") or args.get("end")
            last_completed_at = status_data.get("pipeline", {}).get("finished_at")

            cont = status_data.get("pipeline", {}).get("continuous")
            if cont:
                current_day = cont.get("current_day", current_day)
                waiting = cont.get("waiting", False)
                next_run_at = cont.get("next_run_at")
                days_completed_cont = cont.get("days_completed")
                days_skipped = cont.get("days_skipped", [])
        except (json.JSONDecodeError, OSError):
            pass

    done = min(days_ingested, days_processed, days_detected, days_associated)
    percent = round((done / total_days) * 100, 1) if total_days > 0 else 0

    return {
        "start_date": PIPELINE_START_DATE.isoformat(),
        "target_date": target_date.isoformat(),
        "total_days": total_days,
        "days_ingested": days_ingested,
        "days_processed": days_processed,
        "days_detected": days_detected,
        "days_associated": days_associated,
        "days_complete": done,
        "percent": percent,
        "current_day": current_day,
        "pipeline_status": pipeline_status,
        "mode": mode,
        "waiting": waiting,
        "next_run_at": next_run_at,
        "days_completed_continuous": days_completed_cont,
        "days_skipped": days_skipped,
        "last_completed_at": last_completed_at,
        "last_ingested": last_ingested,
        "last_processed": last_processed,
        "last_detected": last_detected,
        "last_associated": last_associated,
    }


# ── Pipeline control endpoints ────────────────────────────────────


def _is_pipeline_running() -> tuple[bool, int | None]:
    """Check if the pipeline process is running.

    First checks the in-memory subprocess handle, then falls back to the PID
    file for orphan recovery (e.g. after a dashboard restart).
    """
    global _pipeline_proc

    # 1. Check in-memory handle
    if _pipeline_proc is not None:
        if _pipeline_proc.poll() is None:
            return True, _pipeline_proc.pid
        # Process finished — clean up
        _pipeline_proc = None
        PIPELINE_PID_FILE.unlink(missing_ok=True)

    # 2. Fall back to PID file (orphan recovery)
    if PIPELINE_PID_FILE.exists():
        try:
            pid = int(PIPELINE_PID_FILE.read_text().strip())
            os.kill(pid, 0)  # probe — doesn't actually send a signal
            return True, pid
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale PID file — clean up
            PIPELINE_PID_FILE.unlink(missing_ok=True)

    return False, None


@app.get("/api/pipeline/running")
async def pipeline_running():
    running, pid = _is_pipeline_running()
    return {"running": running, "pid": pid}


@app.post("/api/pipeline/start")
async def pipeline_start(request: Request):
    global _pipeline_proc

    running, _ = _is_pipeline_running()
    if running:
        raise HTTPException(status_code=409, detail="Pipeline is already running")

    body = await request.json()
    mode = body.get("mode", "continuous")
    if mode not in ("continuous", "backfill"):
        raise HTTPException(status_code=400, detail="mode must be 'continuous' or 'backfill'")

    cmd = [sys.executable, str(PIPELINE_SCRIPT)]

    if mode == "continuous":
        cmd.append("--continuous")
    else:
        # backfill = single-run with start/end dates
        start = body.get("start")
        end = body.get("end")
        if not start or not end:
            raise HTTPException(status_code=400, detail="backfill mode requires start and end dates")
        # validate dates
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid date format (use YYYY-MM-DD)")
        cmd += ["--start", start, "--end", end]

    if body.get("force"):
        cmd.append("--force")

    # Open log file for stdout/stderr
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(LOGS_DIR / "pipeline_stdout.log", "a")

    _pipeline_proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Write PID file
    PIPELINE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIPELINE_PID_FILE.write_text(str(_pipeline_proc.pid))

    return {"started": True, "pid": _pipeline_proc.pid, "mode": mode}


@app.post("/api/pipeline/stop")
async def pipeline_stop():
    global _pipeline_proc

    running, pid = _is_pipeline_running()
    if not running or pid is None:
        raise HTTPException(status_code=404, detail="No pipeline process is running")

    # Send SIGTERM — run_pipeline.py already handles this gracefully
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # Clean up
    _pipeline_proc = None
    PIPELINE_PID_FILE.unlink(missing_ok=True)

    return {"stopped": True, "pid": pid}


# Regex for standard Python logging lines
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(ERROR|CRITICAL|WARNING)\s+(.+)$"
)


@app.get("/api/errors")
@app.get("/api/v1/errors")
async def errors(limit: int = Query(default=50, ge=1, le=500)):
    entries: list[dict] = []

    for step_name in ("ingest", "process", "detect", "associate", "catalog"):
        log_path = LOGS_DIR / f"{step_name}.log"
        if not log_path.exists():
            continue
        try:
            for line in log_path.read_text().splitlines():
                m = _LOG_LINE_RE.match(line)
                if m:
                    entries.append({
                        "timestamp": m.group(1),
                        "level": m.group(2),
                        "step": step_name,
                        "message": m.group(3),
                    })
        except OSError:
            pass

    if STATUS_FILE.exists():
        try:
            status_data = json.loads(STATUS_FILE.read_text())
            for s in status_data.get("steps", []):
                if s.get("status") == "failed":
                    entries.append({
                        "timestamp": s.get("finished_at", ""),
                        "level": "CRITICAL",
                        "step": s["name"],
                        "message": f"Step {s['name']} failed (exit code {s.get('return_code')})",
                    })
        except (json.JSONDecodeError, OSError):
            pass

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"errors": entries[:limit]}


@app.get("/api/throughput")
@app.get("/api/v1/throughput")
async def throughput():
    empty = {
        "avg_day_sec": None, "last_day_sec": None,
        "step_avg_sec": {}, "remaining_days": 0,
        "eta_sec": None, "sample_size": 0,
    }
    if not STATUS_FILE.exists():
        return empty
    try:
        status_data = json.loads(STATUS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return empty

    cont = status_data.get("pipeline", {}).get("continuous")
    if not cont:
        return empty

    day_times = cont.get("day_times", [])
    totals = [d["total"] for d in day_times if d.get("total")]
    if not totals:
        return empty

    avg_day = sum(totals) / len(totals)
    last_day = totals[-1]

    step_sums: dict[str, float] = {}
    step_n: dict[str, int] = {}
    for d in day_times:
        for name, sec in d.get("steps", {}).items():
            step_sums[name] = step_sums.get(name, 0) + sec
            step_n[name] = step_n.get(name, 0) + 1
    step_avg = {
        name: round(step_sums[name] / step_n[name], 1)
        for name in step_sums
    }

    target = (datetime.now(timezone.utc) - timedelta(hours=LAG_HOURS)).date()
    current_day_str = cont.get("current_day")
    remaining_days = 0
    if current_day_str:
        try:
            cd = date.fromisoformat(current_day_str)
            remaining_days = max((target - cd).days + 1, 0)
        except ValueError:
            pass
    eta_sec = round(remaining_days * avg_day) if avg_day else None

    return {
        "avg_day_sec": round(avg_day, 1),
        "last_day_sec": round(last_day, 1),
        "step_avg_sec": step_avg,
        "remaining_days": remaining_days,
        "eta_sec": eta_sec,
        "sample_size": len(totals),
    }


@app.get("/api/station_health")
@app.get("/api/v1/station_health")
async def station_health():
    stations_data = _load_stations()

    day_dirs: list[Path] = []
    if RAW_DIR.exists():
        for year_dir in sorted(RAW_DIR.iterdir(), reverse=True):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for doy_dir in sorted(year_dir.iterdir(), reverse=True):
                if not doy_dir.is_dir() or not doy_dir.name.isdigit():
                    continue
                if sum(1 for _ in doy_dir.glob("*.mseed")) < 3:
                    continue
                day_dirs.append(doy_dir)
                if len(day_dirs) >= 3:
                    break
            if len(day_dirs) >= 3:
                break

    if not day_dirs:
        return [
            {"station": s["station"], "network": s["network"],
             "status": "unknown", "files_recent": 0}
            for s in stations_data
        ]

    results = []
    for s in stations_data:
        prefix = f"{s['network']}.{s['station']}."
        found = 0
        for d in day_dirs:
            if any(d.glob(f"{prefix}*.mseed")):
                found += 1
        if found == len(day_dirs):
            health_status = "ok"
        elif found > 0:
            health_status = "warning"
        else:
            health_status = "error"
        results.append({
            "station": s["station"],
            "network": s["network"],
            "status": health_status,
            "files_recent": found,
        })
    return results


@app.get("/api/event_rate")
@app.get("/api/v1/event_rate")
async def event_rate(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    rows = _read_catalog()
    if start_date or end_date:
        rows = _filter_by_date(rows, start_date, end_date)
    counts: dict[str, int] = {}
    for r in rows:
        t = r.get("time", "")
        if len(t) >= 10:
            day = t[:10]
            counts[day] = counts.get(day, 0) + 1
    days = [{"date": d, "count": c} for d, c in sorted(counts.items())]
    return {"days": days}


@app.get("/api/data_completeness")
@app.get("/api/v1/data_completeness")
async def data_completeness():
    stations_data = _load_stations()
    station_names = [s["station"] for s in stations_data]

    counts: dict[str, dict[str, int]] = {}
    if RAW_DIR.exists():
        for year_dir in RAW_DIR.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            year = int(year_dir.name)
            for doy_dir in year_dir.iterdir():
                if not doy_dir.is_dir() or not doy_dir.name.isdigit():
                    continue
                doy = int(doy_dir.name)
                day_iso = (date(year, 1, 1) + timedelta(days=doy - 1)).isoformat()
                for f in doy_dir.iterdir():
                    if not f.name.endswith(".mseed"):
                        continue
                    parts = f.name.split(".")
                    if len(parts) >= 2:
                        sta = parts[1]
                        if sta not in counts:
                            counts[sta] = {}
                        counts[sta][day_iso] = counts[sta].get(day_iso, 0) + 1

    all_days = sorted({d for per_sta in counts.values() for d in per_sta})
    matrix = []
    for sta in station_names:
        row = [counts.get(sta, {}).get(d, 0) for d in all_days]
        matrix.append(row)

    return {"stations": station_names, "days": all_days, "matrix": matrix}


@app.get("/api/pick_quality")
@app.get("/api/v1/pick_quality")
async def pick_quality():
    bins = [0] * 10
    total = 0
    p_count = 0
    s_count = 0

    for pf in PICKS_DIR.rglob("*.picks.csv"):
        try:
            with open(pf, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    prob = float(row.get("probability", 0))
                    phase = row.get("phase", "")
                    idx = min(int(prob * 10), 9)
                    bins[idx] += 1
                    total += 1
                    if phase == "P":
                        p_count += 1
                    elif phase == "S":
                        s_count += 1
        except (OSError, ValueError):
            pass

    return {
        "bins": bins,
        "bin_edges": [round(i * 0.1, 1) for i in range(11)],
        "total": total,
        "p_count": p_count,
        "s_count": s_count,
    }


@app.get("/api/station_picks")
@app.get("/api/v1/station_picks")
async def station_picks():
    stations_data = _load_stations()
    station_names = [s["station"] for s in stations_data]

    counts: dict[str, dict[str, int]] = {}
    for pf in PICKS_DIR.rglob("*.picks.csv"):
        try:
            with open(pf, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sta = row.get("station", "")
                    t = row.get("time", "")
                    if sta and len(t) >= 10:
                        day = t[:10]
                        if sta not in counts:
                            counts[sta] = {}
                        counts[sta][day] = counts[sta].get(day, 0) + 1
        except OSError:
            pass

    all_days = sorted({d for per_sta in counts.values() for d in per_sta})

    matrix = []
    for sta in station_names:
        row = [counts.get(sta, {}).get(d, 0) for d in all_days]
        matrix.append(row)

    return {
        "stations": station_names,
        "days": all_days,
        "matrix": matrix,
    }


@app.get("/api/event/{event_id}")
@app.get("/api/v1/event/{event_id}")
async def event_detail(event_id: str):
    """Get detailed information for a single event including picks and station contributions."""
    rows = _read_catalog()
    event_row = None
    for r in rows:
        if r.get("event_id") == event_id:
            event_row = r
            break
    if event_row is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    event = _parse_catalog_row(event_row)

    # Find picks via assignments CSV (authoritative event-to-pick linkage)
    event_picks = []
    assignments = _read_assignments()
    for a in assignments:
        if a.get("event_id") == event_id:
            event_picks.append({
                "network": a.get("network", ""),
                "station": a.get("station", ""),
                "location": a.get("location", ""),
                "channel": a.get("channel", ""),
                "phase": a.get("phase", ""),
                "time": a.get("time", ""),
                "probability": float(a["probability"]) if a.get("probability") else None,
                "amplitude": float(a["amplitude"]) if a.get("amplitude") else None,
            })

    # Station contributions
    station_counts: dict[str, dict[str, int]] = {}
    for pick in event_picks:
        sta = pick["station"]
        phase = pick["phase"]
        if sta not in station_counts:
            station_counts[sta] = {"P": 0, "S": 0}
        if phase in station_counts[sta]:
            station_counts[sta][phase] += 1

    return {
        "event": event,
        "picks": event_picks,
        "station_contributions": station_counts,
    }


@app.get("/api/catalog/export")
@app.get("/api/v1/catalog/export")
async def catalog_export(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Export filtered catalog as CSV."""
    rows = _read_catalog()
    if start_date or end_date:
        rows = _filter_by_date(rows, start_date, end_date)

    if not rows:
        raise HTTPException(status_code=404, detail="No catalog data available for the selected date range")

    import io
    output = io.StringIO()
    fieldnames = [
        "event_id", "time", "magnitude", "magnitude_type", "ml_err",
        "latitude", "longitude", "depth_km", "sigma_time", "sigma_amp",
        "num_picks", "num_ml_sta", "reviewed", "review_status",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})

    return PlainTextResponse(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=catalog_export.csv"},
    )


# ── Magnitude-Frequency (Gutenberg-Richter) ──────────────────────
@app.get("/api/magnitude_frequency")
async def magnitude_frequency(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    bin_width: float = Query(default=0.1, ge=0.01, le=1.0),
):
    """Gutenberg-Richter magnitude-frequency analysis with b-value estimation."""
    import math

    rows = _read_catalog()
    if start_date or end_date:
        rows = _filter_by_date(rows, start_date, end_date)

    magnitudes = [float(r["magnitude"]) for r in rows if r.get("magnitude")]
    if len(magnitudes) < 2:
        return {"bins": [], "cumulative": [], "b_value": None, "a_value": None,
                "mc": None, "r_squared": None, "total_events": len(magnitudes)}

    mag_min = math.floor(min(magnitudes) / bin_width) * bin_width
    mag_max = math.ceil(max(magnitudes) / bin_width) * bin_width
    n_bins = max(1, int(round((mag_max - mag_min) / bin_width)))

    bins = []
    counts = []
    for i in range(n_bins):
        edge_lo = round(mag_min + i * bin_width, 4)
        edge_hi = round(edge_lo + bin_width, 4)
        c = sum(1 for m in magnitudes if edge_lo <= m < edge_hi)
        bins.append(round(edge_lo + bin_width / 2, 4))
        counts.append(c)
    # Include upper edge in last bin
    if magnitudes:
        c_last = sum(1 for m in magnitudes if m >= round(mag_min + (n_bins - 1) * bin_width, 4))
        counts[-1] = c_last

    # Cumulative counts (N >= M)
    cumulative = []
    running = 0
    for i in range(len(counts) - 1, -1, -1):
        running += counts[i]
        cumulative.append(running)
    cumulative.reverse()

    log10_n = [round(math.log10(c), 4) if c > 0 else None for c in cumulative]

    # Mc via maximum curvature (bin with highest non-cumulative count)
    mc_idx = counts.index(max(counts))
    mc = bins[mc_idx]

    # b-value regression on M >= Mc using least-squares
    fit_m = []
    fit_logn = []
    for i, b in enumerate(bins):
        if b >= mc and cumulative[i] > 0:
            fit_m.append(b)
            fit_logn.append(math.log10(cumulative[i]))

    b_value = None
    a_value = None
    r_squared = None
    if len(fit_m) >= 2:
        n = len(fit_m)
        sx = sum(fit_m)
        sy = sum(fit_logn)
        sxx = sum(x * x for x in fit_m)
        sxy = sum(x * y for x, y in zip(fit_m, fit_logn))
        denom = n * sxx - sx * sx
        if abs(denom) > 1e-12:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n
            b_value = round(-slope, 3)
            a_value = round(intercept, 3)
            # R-squared
            y_mean = sy / n
            ss_tot = sum((y - y_mean) ** 2 for y in fit_logn)
            ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(fit_m, fit_logn))
            r_squared = round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None

    return {
        "bins": bins,
        "counts": counts,
        "cumulative": cumulative,
        "log10_n": log10_n,
        "b_value": b_value,
        "a_value": a_value,
        "mc": round(mc, 4),
        "r_squared": r_squared,
        "total_events": len(magnitudes),
        "bin_width": bin_width,
    }


# ── Depth Distribution ───────────────────────────────────────────
@app.get("/api/depth_distribution")
async def depth_distribution(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    bin_size: float = Query(default=1.0, ge=0.1, le=10.0),
):
    """Depth distribution histogram and cross-section scatter data."""
    import math

    rows = _read_catalog()
    if start_date or end_date:
        rows = _filter_by_date(rows, start_date, end_date)

    events = [_parse_catalog_row(r) for r in rows]
    events = [e for e in events if e["depth_km"] is not None]

    if not events:
        return {"histogram": {"bins": [], "counts": []}, "median_depth": None,
                "mean_depth": None, "scatter": []}

    depths = [e["depth_km"] for e in events]
    median_depth = round(sorted(depths)[len(depths) // 2], 2)
    mean_depth = round(sum(depths) / len(depths), 2)

    d_min = math.floor(min(depths) / bin_size) * bin_size
    d_max = math.ceil(max(depths) / bin_size) * bin_size
    n_bins = max(1, int(round((d_max - d_min) / bin_size)))

    hist_bins = []
    hist_counts = []
    for i in range(n_bins):
        edge_lo = round(d_min + i * bin_size, 4)
        edge_hi = round(edge_lo + bin_size, 4)
        c = sum(1 for d in depths if edge_lo <= d < edge_hi)
        hist_bins.append(round(edge_lo + bin_size / 2, 4))
        hist_counts.append(c)
    # Include upper edge in last bin
    last_edge = round(d_min + (n_bins - 1) * bin_size, 4)
    hist_counts[-1] = sum(1 for d in depths if d >= last_edge)

    scatter = [
        {
            "latitude": e["latitude"],
            "longitude": e["longitude"],
            "depth_km": e["depth_km"],
            "magnitude": e["magnitude"],
            "event_id": e["event_id"],
        }
        for e in events if e["latitude"] is not None and e["longitude"] is not None
    ]

    return {
        "histogram": {"bins": hist_bins, "counts": hist_counts, "bin_size": bin_size},
        "median_depth": median_depth,
        "mean_depth": mean_depth,
        "scatter": scatter,
    }


# ── Waveform API ─────────────────────────────────────────────────
_waveform_cache: dict[str, tuple[float, object]] = {}
_WAVEFORM_CACHE_MAX = 10
_WAVEFORM_MAX_DURATION = 600  # seconds


def _get_obspy_stream(file_path: Path):
    """Read a miniSEED file with LRU caching."""
    key = str(file_path)
    mtime = _file_mtime(file_path)
    if key in _waveform_cache:
        cached_mtime, cached_stream = _waveform_cache[key]
        if cached_mtime == mtime:
            return cached_stream
    try:
        from obspy import read as obspy_read
        st = obspy_read(str(file_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read {file_path.name}: {exc}")
    # Evict oldest if cache full
    if len(_waveform_cache) >= _WAVEFORM_CACHE_MAX:
        oldest_key = next(iter(_waveform_cache))
        del _waveform_cache[oldest_key]
    _waveform_cache[key] = (mtime, st)
    return st


@app.get("/api/waveform")
async def waveform(
    network: str = Query(...),
    station: str = Query(...),
    channel: str = Query(default="EHZ"),
    location: str = Query(default="00"),
    start: str = Query(..., description="ISO 8601 start time"),
    end: str = Query(..., description="ISO 8601 end time"),
    max_samples: int = Query(default=10000, ge=100, le=100000),
):
    """Return waveform data for a single trace from miniSEED files."""
    from obspy import UTCDateTime

    t_start = UTCDateTime(start)
    t_end = UTCDateTime(end)
    duration = t_end - t_start
    if duration <= 0 or duration > _WAVEFORM_MAX_DURATION:
        raise HTTPException(status_code=400, detail=f"Duration must be 0-{_WAVEFORM_MAX_DURATION}s, got {duration:.0f}s")

    # Find the appropriate miniSEED file in processed dir
    jday = t_start.julday
    year = t_start.year
    day_dir = PROCESSED_DIR / str(year) / str(jday).zfill(3)
    if not day_dir.exists():
        raise HTTPException(status_code=404, detail=f"No data for {year}/{jday:03d}")

    pattern = f"{network}.{station}.{location}.{channel}.{year}.{jday:03d}.mseed"
    mseed_file = day_dir / pattern
    if not mseed_file.exists():
        # Try matching any file for this station (exact channel, then any band code)
        candidates = list(day_dir.glob(f"{network}.{station}.*.{channel}.*.mseed"))
        if not candidates and len(channel) == 3:
            candidates = list(day_dir.glob(f"{network}.{station}.*.?{channel[1:]}.*.mseed"))
        if not candidates:
            raise HTTPException(status_code=404, detail=f"No miniSEED for {network}.{station}.{channel}")
        mseed_file = candidates[0]

    st = _get_obspy_stream(mseed_file)
    st_sliced = st.copy().trim(t_start, t_end)

    if len(st_sliced) == 0:
        raise HTTPException(status_code=404, detail="No data in requested time window")

    tr = st_sliced[0]
    data = tr.data.tolist()

    # Downsample if too many samples
    if len(data) > max_samples:
        step = len(data) // max_samples
        data = data[::step]

    return {
        "data": data,
        "sampling_rate": tr.stats.sampling_rate,
        "starttime": str(tr.stats.starttime),
        "endtime": str(tr.stats.endtime),
        "npts": tr.stats.npts,
        "network": tr.stats.network,
        "station": tr.stats.station,
        "channel": tr.stats.channel,
        "location": tr.stats.location,
    }


@app.get("/api/waveforms/all")
async def waveforms_all(
    start: str = Query(..., description="ISO 8601 start time"),
    end: str = Query(..., description="ISO 8601 end time"),
    channel: str = Query(default="EHZ"),
    max_samples: int = Query(default=10000, ge=100, le=100000),
    freqmin: Optional[float] = Query(default=None, ge=0.01, le=50.0),
    freqmax: Optional[float] = Query(default=None, ge=0.1, le=50.0),
    spectrogram: bool = Query(default=False),
):
    """Return waveforms for ALL stations in an arbitrary time window."""
    from obspy import UTCDateTime

    t_start = UTCDateTime(start)
    t_end = UTCDateTime(end)
    duration = t_end - t_start
    if duration <= 0 or duration > _WAVEFORM_MAX_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Duration must be 0-{_WAVEFORM_MAX_DURATION}s, got {duration:.0f}s",
        )

    jday = t_start.julday
    year = t_start.year
    day_dir = PROCESSED_DIR / str(year) / str(jday).zfill(3)

    # Load raw picks for the day and filter to the requested time window
    picks_file = PICKS_DIR / str(year) / str(jday).zfill(3) / f"{year}.{jday:03d}.picks.csv"
    picks_by_station: dict[str, list[dict]] = {}
    if picks_file.exists():
        start_iso = str(t_start).replace("T", " ").rstrip("Z")
        end_iso = str(t_end).replace("T", " ").rstrip("Z")
        with open(picks_file, newline="") as f:
            for row in csv.DictReader(f):
                pick_time = row.get("time", "")
                if not pick_time:
                    continue
                # Quick string comparison for time window filtering
                pt = pick_time.replace("T", " ")
                if pt < start_iso or pt > end_iso:
                    continue
                sta_key = f"{row.get('network', '')}.{row.get('station', '')}"
                if sta_key not in picks_by_station:
                    picks_by_station[sta_key] = []
                picks_by_station[sta_key].append({
                    "phase": row.get("phase", ""),
                    "time": pick_time,
                    "probability": float(row["probability"]) if row.get("probability") else None,
                    "channel": row.get("channel", ""),
                })

    all_stations = _load_stations()
    traces = []

    channels_to_load = ["EHZ", "EHN", "EHE"] if channel == "3C" else [channel]

    for s in all_stations:
        net = s["network"]
        sta = s["station"]
        sta_lat = s.get("latitude")
        sta_lon = s.get("longitude")
        sta_key = f"{net}.{sta}"
        picks = picks_by_station.get(sta_key, [])

        for chan in channels_to_load:
            candidates = list(day_dir.glob(f"{net}.{sta}.*.{chan}.*.mseed")) if day_dir.exists() else []
            if not candidates and day_dir.exists() and len(chan) == 3:
                candidates = list(day_dir.glob(f"{net}.{sta}.*.?{chan[1:]}.*.mseed"))

            if not candidates:
                traces.append({
                    "station": sta, "network": net, "channel": chan,
                    "latitude": sta_lat, "longitude": sta_lon,
                    "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                    "picks": picks, "has_data": False, "error": "No miniSEED file found",
                })
                continue

            try:
                stream = _get_obspy_stream(candidates[0])
                st_sliced = stream.copy().trim(t_start, t_end)
                if len(st_sliced) == 0:
                    traces.append({
                        "station": sta, "network": net, "channel": chan,
                        "latitude": sta_lat, "longitude": sta_lon,
                        "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                        "picks": picks, "has_data": False, "error": "No data in time window",
                    })
                    continue

                tr = st_sliced[0]

                if freqmin is not None and freqmax is not None:
                    tr = tr.copy()
                    tr.detrend("demean")
                    tr.filter("bandpass", freqmin=freqmin, freqmax=freqmax, corners=4, zerophase=True)
                elif freqmin is not None:
                    tr = tr.copy()
                    tr.detrend("demean")
                    tr.filter("highpass", freq=freqmin, corners=4, zerophase=True)
                elif freqmax is not None:
                    tr = tr.copy()
                    tr.detrend("demean")
                    tr.filter("lowpass", freq=freqmax, corners=4, zerophase=True)

                spec_b64 = ""
                if spectrogram and len(tr.data) >= 32:
                    spec_b64 = _compute_spectrogram_png(
                        tr.data.tolist(), tr.stats.sampling_rate,
                        width=1200, height=160,
                    )

                data = tr.data.tolist()
                if len(data) > max_samples:
                    step = len(data) // max_samples
                    data = data[::step]

                trace_dict = {
                    "station": sta, "network": net, "channel": tr.stats.channel,
                    "latitude": sta_lat, "longitude": sta_lon,
                    "data": data,
                    "sampling_rate": tr.stats.sampling_rate,
                    "starttime": str(tr.stats.starttime),
                    "endtime": str(tr.stats.endtime),
                    "picks": picks,
                    "has_data": True,
                }
                if spectrogram and spec_b64:
                    trace_dict["spectrogram_b64"] = spec_b64
                traces.append(trace_dict)
            except Exception as exc:
                traces.append({
                    "station": sta, "network": net, "channel": chan,
                    "latitude": sta_lat, "longitude": sta_lon,
                    "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                    "picks": picks, "has_data": False, "error": str(exc),
                })

    return {
        "start": str(t_start),
        "end": str(t_end),
        "traces": traces,
    }


def _compute_spectrogram_png(
    data: list[float],
    sampling_rate: float,
    width: int = 800,
    height: int = 128,
) -> str:
    """Compute spectrogram and return as Base64-encoded PNG."""
    import base64
    import io
    import numpy as np
    from scipy.signal import spectrogram as sp_spectrogram

    arr = np.array(data, dtype=np.float64)
    if len(arr) < 32:
        return ""

    # STFT parameters tuned for 100 Hz seismic data
    nperseg = min(128, len(arr) // 4)
    noverlap = int(nperseg * 0.75)
    f, t, Sxx = sp_spectrogram(arr, fs=sampling_rate,
                                nperseg=nperseg, noverlap=noverlap)

    # Log-scale power, clip to usable dynamic range
    Sxx_log = 10 * np.log10(Sxx + 1e-30)
    vmin = np.percentile(Sxx_log, 5)
    vmax = np.percentile(Sxx_log, 99)
    Sxx_norm = np.clip((Sxx_log - vmin) / (vmax - vmin + 1e-10), 0, 1)

    # Apply viridis colormap manually (avoid matplotlib import overhead)
    # 5-stop viridis approximation: dark purple -> teal -> yellow
    viridis_lut = np.array([
        [68, 1, 84],    [59, 82, 139],  [33, 145, 140],
        [94, 201, 98],  [253, 231, 37],
    ], dtype=np.uint8)
    idx = (Sxx_norm * (len(viridis_lut) - 1)).astype(int)
    # Flip frequency axis (high freq at top)
    img = viridis_lut[idx[::-1, :]]

    # Resize to target dimensions via PIL
    from PIL import Image
    pil_img = Image.fromarray(img, mode='RGB')
    pil_img = pil_img.resize((width, height), Image.BILINEAR)

    buf = io.BytesIO()
    pil_img.save(buf, format='PNG', optimize=True)
    return base64.b64encode(buf.getvalue()).decode('ascii')


@app.get("/api/event/{event_id}/waveforms")
async def event_waveforms(
    event_id: str,
    channel: str = Query(default="EHZ"),
    window_before: float = Query(default=5.0, ge=0, le=60),
    window_after: float = Query(default=30.0, ge=5, le=300),
    max_samples: int = Query(default=5000, ge=100, le=50000),
    spectrogram: bool = Query(default=False),
):
    """Return waveform data + picks for all stations contributing to an event."""
    from obspy import UTCDateTime

    # Get event info
    rows = _read_catalog()
    event_row = None
    for r in rows:
        if r.get("event_id") == event_id:
            event_row = r
            break
    if event_row is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    event_time = event_row.get("time", "")
    if not event_time:
        raise HTTPException(status_code=400, detail="Event has no time")

    # Get assignments for this event
    assignments = _read_assignments()
    event_assignments = [a for a in assignments if a.get("event_id") == event_id]
    if not event_assignments:
        raise HTTPException(status_code=404, detail=f"No pick assignments for {event_id}")

    # Find unique stations
    stations_seen: dict[str, list[dict]] = {}
    for a in event_assignments:
        sta_key = f"{a.get('network', '')}.{a.get('station', '')}"
        if sta_key not in stations_seen:
            stations_seen[sta_key] = []
        stations_seen[sta_key].append({
            "phase": a.get("phase", ""),
            "time": a.get("time", ""),
            "probability": float(a["probability"]) if a.get("probability") else None,
            "channel": a.get("channel", ""),
        })

    t_origin = UTCDateTime(event_time)
    t_start = t_origin - window_before
    t_end = t_origin + window_after
    jday = t_origin.julday
    year = t_origin.year
    day_dir = PROCESSED_DIR / str(year) / str(jday).zfill(3)

    traces = []
    for sta_key, picks in stations_seen.items():
        parts = sta_key.split(".")
        if len(parts) != 2:
            continue
        net, sta = parts

        # Find miniSEED file — try exact channel, then any band code with
        # same instrument+orientation (e.g. EHZ→HHZ) for mixed networks
        candidates = list(day_dir.glob(f"{net}.{sta}.*.{channel}.*.mseed")) if day_dir.exists() else []
        if not candidates and day_dir.exists() and len(channel) == 3:
            candidates = list(day_dir.glob(f"{net}.{sta}.*.?{channel[1:]}.*.mseed"))
        if not candidates:
            traces.append({
                "station": sta, "network": net, "channel": channel,
                "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                "picks": picks, "error": "No miniSEED file found",
            })
            continue

        try:
            st = _get_obspy_stream(candidates[0])
            st_sliced = st.copy().trim(t_start, t_end)
            if len(st_sliced) == 0:
                traces.append({
                    "station": sta, "network": net, "channel": channel,
                    "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                    "picks": picks, "error": "No data in time window",
                })
                continue

            tr = st_sliced[0]

            # Compute spectrogram on full-res data before downsampling
            spec_b64 = ""
            if spectrogram and len(tr.data) >= 32:
                spec_b64 = _compute_spectrogram_png(
                    tr.data.tolist(), tr.stats.sampling_rate,
                    width=800, height=128,
                )

            data = tr.data.tolist()
            if len(data) > max_samples:
                step = len(data) // max_samples
                data = data[::step]

            trace_dict = {
                "station": sta, "network": net, "channel": channel,
                "data": data,
                "sampling_rate": tr.stats.sampling_rate,
                "starttime": str(tr.stats.starttime),
                "endtime": str(tr.stats.endtime),
                "picks": picks,
            }
            if spectrogram and spec_b64:
                trace_dict["spectrogram_b64"] = spec_b64
            traces.append(trace_dict)
        except Exception as exc:
            traces.append({
                "station": sta, "network": net, "channel": channel,
                "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                "picks": picks, "error": str(exc),
            })

    return {
        "event_id": event_id,
        "event_time": event_time,
        "window_before": window_before,
        "window_after": window_after,
        "traces": traces,
    }


# ── QuakeML Export ───────────────────────────────────────────────
@app.get("/api/catalog/export/quakeml")
async def catalog_export_quakeml(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Export catalog as QuakeML using ObsPy."""
    from obspy import UTCDateTime
    from obspy.core.event import (
        Catalog as ObsCatalog,
        Event as ObsEvent,
        Magnitude,
        Origin,
        Pick as ObsPick,
        WaveformStreamID,
    )
    from starlette.responses import Response

    rows = _read_catalog()
    if start_date or end_date:
        rows = _filter_by_date(rows, start_date, end_date)

    if not rows:
        raise HTTPException(status_code=404, detail="No catalog data for the selected date range")

    assignments = _read_assignments()
    # Index assignments by event_id
    assign_by_event: dict[str, list[dict]] = {}
    for a in assignments:
        eid = a.get("event_id", "")
        if eid:
            if eid not in assign_by_event:
                assign_by_event[eid] = []
            assign_by_event[eid].append(a)

    obs_catalog = ObsCatalog()
    for r in rows:
        event_id = r.get("event_id", "")
        t = r.get("time", "")
        if not t:
            continue

        origin = Origin(
            time=UTCDateTime(t),
            latitude=float(r["latitude"]) if r.get("latitude") else None,
            longitude=float(r["longitude"]) if r.get("longitude") else None,
            depth=float(r["depth_km"]) * 1000 if r.get("depth_km") else None,
        )

        ev = ObsEvent(
            resource_id=f"smi:elpaso/{event_id}",
            origins=[origin],
            preferred_origin_id=origin.resource_id,
        )

        if r.get("magnitude"):
            mag = Magnitude(
                mag=float(r["magnitude"]),
                magnitude_type=r.get("magnitude_type", "ML") or "ML",
                origin_id=origin.resource_id,
            )
            ev.magnitudes.append(mag)
            ev.preferred_magnitude_id = mag.resource_id

        # Add picks from assignments
        for a in assign_by_event.get(event_id, []):
            pick_time = a.get("time", "")
            if not pick_time:
                continue
            wf_id = WaveformStreamID(
                network_code=a.get("network", ""),
                station_code=a.get("station", ""),
                location_code=a.get("location", ""),
                channel_code=a.get("channel", ""),
            )
            pick = ObsPick(
                time=UTCDateTime(pick_time),
                phase_hint=a.get("phase", ""),
                waveform_id=wf_id,
            )
            ev.picks.append(pick)

        obs_catalog.append(ev)

    import io
    buf = io.BytesIO()
    obs_catalog.write(buf, format="QUAKEML")
    xml_bytes = buf.getvalue()

    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=catalog_export.xml"},
    )


# ── Atomic CSV writer ────────────────────────────────────────────
def _atomic_write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    """Write CSV via temp file + os.replace for crash safety."""
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


# ── GaMMA initialisation (lazy, cached) ──────────────────────────
_gamma_state: dict | None = None


def _init_gamma() -> dict:
    """Lazily initialise and cache GaMMA station DataFrame, projection, and config."""
    global _gamma_state
    if _gamma_state is not None:
        return _gamma_state

    # sklearn shim (same as associate.py)
    import sklearn.base
    if not hasattr(sklearn.base.BaseEstimator, "_check_n_features"):
        def _check_n_features(self, X, reset=False):
            n_features = X.shape[1] if hasattr(X, "shape") and X.ndim > 1 else 1
            if reset or not hasattr(self, "n_features_in_"):
                self.n_features_in_ = n_features
        sklearn.base.BaseEstimator._check_n_features = _check_n_features

    import numpy as np
    import pandas as pd
    from lib.projection import make_projection

    stations = _load_stations()
    center_lon, center_lat = -106.40, 31.85
    proj = make_projection(center_lon, center_lat)

    sta_rows = []
    sta_lookup = {}
    for s in stations:
        sid = f"{s['network']}.{s['station']}"
        x_km, y_km = proj(s["longitude"], s["latitude"])
        z_km = -s["elevation_m"] / 1000.0
        sta_rows.append({"id": sid, "x(km)": x_km, "y(km)": y_km, "z(km)": z_km})
        sta_lookup[sid] = {
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "elevation_m": s["elevation_m"],
        }

    stations_df = pd.DataFrame(sta_rows)

    xlim, ylim = 0.7, 0.7
    degree2km = 111.19
    x_km_lim = np.array([-xlim, xlim]) * degree2km * np.cos(np.deg2rad(center_lat))
    y_km_lim = np.array([-ylim, ylim]) * degree2km
    dims = ["x(km)", "y(km)", "z(km)"]

    gamma_config = {
        "center": (center_lon, center_lat),
        "xlim_degree": [-xlim, xlim],
        "ylim_degree": [-ylim, ylim],
        "degree2km": degree2km,
        "dims": dims,
        "x(km)": x_km_lim.tolist(),
        "y(km)": y_km_lim.tolist(),
        "z(km)": [0, 30],
        "use_amplitude": False,
        "vel": {"p": 5.0, "s": 2.89},
        "use_dbscan": True,
        "dbscan_eps": 25,
        "dbscan_min_samples": 3,
        "min_picks_per_eq": 4,
        "max_sigma11": 2.0,
        "max_sigma22": 2.0,
        "oversample_factor": 5,
        "bfgs_bounds": (
            [x_km_lim.tolist(), y_km_lim.tolist(), [0, 30]] + [[None, None]]
        ),
    }

    _gamma_state = {
        "stations_df": stations_df,
        "proj": proj,
        "config": gamma_config,
        "station_lookup": sta_lookup,
        "center_lon": center_lon,
        "center_lat": center_lat,
    }
    return _gamma_state


# ── GET /api/event/{event_id}/waveforms/all ──────────────────────
@app.get("/api/event/{event_id}/waveforms/all")
async def event_waveforms_all(
    event_id: str,
    channel: str = Query(default="EHZ"),
    window_before: float = Query(default=10.0, ge=0, le=120),
    window_after: float = Query(default=60.0, ge=5, le=600),
    max_samples: int = Query(default=10000, ge=100, le=100000),
    freqmin: Optional[float] = Query(default=None, ge=0.01, le=50.0),
    freqmax: Optional[float] = Query(default=None, ge=0.1, le=50.0),
    spectrogram: bool = Query(default=False),
):
    """Return waveforms for ALL stations with optional bandpass filter."""
    from obspy import UTCDateTime

    # Get event info
    rows = _read_catalog()
    event_row = None
    for r in rows:
        if r.get("event_id") == event_id:
            event_row = r
            break
    if event_row is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    event_time = event_row.get("time", "")
    if not event_time:
        raise HTTPException(status_code=400, detail="Event has no time")

    # Load existing picks for this event
    assignments = _read_assignments()
    picks_by_station: dict[str, list[dict]] = {}
    for a in assignments:
        if a.get("event_id") == event_id:
            sta_key = f"{a.get('network', '')}.{a.get('station', '')}"
            if sta_key not in picks_by_station:
                picks_by_station[sta_key] = []
            picks_by_station[sta_key].append({
                "phase": a.get("phase", ""),
                "time": a.get("time", ""),
                "probability": float(a["probability"]) if a.get("probability") else None,
                "channel": a.get("channel", ""),
                "amplitude": float(a["amplitude"]) if a.get("amplitude") else None,
            })

    t_origin = UTCDateTime(event_time)
    t_start = t_origin - window_before
    t_end = t_origin + window_after
    jday = t_origin.julday
    year = t_origin.year
    day_dir = PROCESSED_DIR / str(year) / str(jday).zfill(3)

    all_stations = _load_stations()
    traces = []

    # Support 3-component mode
    channels_to_load = ["EHZ", "EHN", "EHE"] if channel == "3C" else [channel]

    for s in all_stations:
        net = s["network"]
        sta = s["station"]
        sta_key = f"{net}.{sta}"
        picks = picks_by_station.get(sta_key, [])
        sta_lat = s.get("latitude")
        sta_lon = s.get("longitude")

        for chan in channels_to_load:
            # Find miniSEED file
            candidates = list(day_dir.glob(f"{net}.{sta}.*.{chan}.*.mseed")) if day_dir.exists() else []
            if not candidates and day_dir.exists() and len(chan) == 3:
                candidates = list(day_dir.glob(f"{net}.{sta}.*.?{chan[1:]}.*.mseed"))

            if not candidates:
                traces.append({
                    "station": sta, "network": net, "channel": chan,
                    "latitude": sta_lat, "longitude": sta_lon,
                    "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                    "picks": picks, "has_data": False, "error": "No miniSEED file found",
                })
                continue

            try:
                stream = _get_obspy_stream(candidates[0])
                st_sliced = stream.copy().trim(t_start, t_end)
                if len(st_sliced) == 0:
                    traces.append({
                        "station": sta, "network": net, "channel": chan,
                        "latitude": sta_lat, "longitude": sta_lon,
                        "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                        "picks": picks, "has_data": False, "error": "No data in time window",
                    })
                    continue

                tr = st_sliced[0]

                # Apply bandpass filter if requested
                if freqmin is not None and freqmax is not None:
                    tr = tr.copy()
                    tr.detrend("demean")
                    tr.filter("bandpass", freqmin=freqmin, freqmax=freqmax, corners=4, zerophase=True)
                elif freqmin is not None:
                    tr = tr.copy()
                    tr.detrend("demean")
                    tr.filter("highpass", freq=freqmin, corners=4, zerophase=True)
                elif freqmax is not None:
                    tr = tr.copy()
                    tr.detrend("demean")
                    tr.filter("lowpass", freq=freqmax, corners=4, zerophase=True)

                # Compute spectrogram on full-res data before downsampling
                spec_b64 = ""
                if spectrogram and len(tr.data) >= 32:
                    spec_b64 = _compute_spectrogram_png(
                        tr.data.tolist(), tr.stats.sampling_rate,
                        width=1200, height=160,
                    )

                data = tr.data.tolist()
                if len(data) > max_samples:
                    step = len(data) // max_samples
                    data = data[::step]

                trace_dict = {
                    "station": sta, "network": net, "channel": tr.stats.channel,
                    "latitude": sta_lat, "longitude": sta_lon,
                    "data": data,
                    "sampling_rate": tr.stats.sampling_rate,
                    "starttime": str(tr.stats.starttime),
                    "endtime": str(tr.stats.endtime),
                    "picks": picks,
                    "has_data": True,
                }
                if spectrogram and spec_b64:
                    trace_dict["spectrogram_b64"] = spec_b64
                traces.append(trace_dict)
            except Exception as exc:
                traces.append({
                    "station": sta, "network": net, "channel": chan,
                    "latitude": sta_lat, "longitude": sta_lon,
                    "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                    "picks": picks, "has_data": False, "error": str(exc),
                })

    ev_lat = float(event_row["latitude"]) if event_row.get("latitude") else None
    ev_lon = float(event_row["longitude"]) if event_row.get("longitude") else None

    return {
        "event_id": event_id,
        "event_time": event_time,
        "event_latitude": ev_lat,
        "event_longitude": ev_lon,
        "traces": traces,
    }


# ── POST /api/event/{event_id}/relocate ──────────────────────────
@app.post("/api/event/{event_id}/relocate")
async def event_relocate(event_id: str, request: Request):
    """Run GaMMA relocation on user-edited picks (no save)."""
    body = await request.json()
    picks_input = body.get("picks", [])
    if len(picks_input) < 4:
        raise HTTPException(status_code=400, detail="Need at least 4 picks for relocation")

    import numpy as np
    import pandas as pd
    from gamma.utils import association
    from lib.magnitude import MLConfig, compute_ml_network, compute_ml_station, haversine_km

    gs = _init_gamma()
    stations_df = gs["stations_df"]
    proj = gs["proj"]
    gamma_config = gs["config"]
    station_lookup = gs["station_lookup"]

    # Build picks DataFrame
    try:
        pick_rows = []
        for p in picks_input:
            raw_amp = p.get("amplitude")
            amp_val = float(raw_amp) if raw_amp and float(raw_amp) > 0 else np.nan
            pick_rows.append({
                "id": f"{p['network']}.{p['station']}",
                "timestamp": pd.Timestamp(p["time"]),
                "type": p["phase"],
                "prob": float(p.get("probability", 0.8)),
                "amp": amp_val,
                "network": p["network"],
                "station": p["station"],
                "channel": p.get("channel", ""),
                "phase": p["phase"],
                "time": p["time"],
                "probability": float(p.get("probability", 0.8)),
                "amplitude": amp_val,
            })
        picks_df = pd.DataFrame(pick_rows)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid pick data: {exc}")

    try:
        events_list, assignments_list = association(
            picks_df, stations_df, gamma_config, method="BGMM",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GaMMA association failed: {exc}")

    if not events_list:
        raise HTTPException(status_code=422, detail="Relocation failed — no event found from these picks")

    try:
        # Use the first (best) event
        ev = events_list[0]
        x = ev.get("x(km)", 0.0)
        y = ev.get("y(km)", 0.0)
        depth = ev.get("z(km)", 0.0)

        # Convert event time robustly — GaMMA may return pd.Timestamp,
        # numpy datetime64, or string; normalise via pd.Timestamp → ISO.
        ev_time_raw = ev.get("time", "")
        ev_ts = pd.Timestamp(ev_time_raw)
        ev_time = ev_ts.isoformat()
        ev_epoch = ev_ts.timestamp()  # Unix seconds for residual math

        lon, lat = proj(x, y, inverse=True)

        # Compute magnitude
        ml_cfg = MLConfig(
            freq_hz=5.0, wa_gain=2800.0, min_distance_km=10.0,
            a=1.110, b=0.00189, c=3.0, ref_distance_km=100.0,
        )
        station_mls = []
        for pick_idx, ev_idx, _score in assignments_list:
            pick = picks_df.iloc[pick_idx]
            amp_vel = pick.get("amplitude", 0.0)
            if amp_vel is None or not np.isfinite(amp_vel) or amp_vel <= 0:
                continue
            sta_id = f"{pick.get('network', '')}.{pick.get('station', '')}"
            sta = station_lookup.get(sta_id)
            if sta is None:
                continue
            d_horiz = haversine_km(lat, lon, sta["latitude"], sta["longitude"])
            r = math.sqrt(d_horiz ** 2 + depth ** 2)
            ml_sta = compute_ml_station(float(amp_vel), r, ml_cfg)
            if ml_sta is not None:
                station_mls.append(ml_sta)

        ml, ml_err, ml_count = compute_ml_network(station_mls)

        # Compute residuals using epoch arithmetic (avoids UTCDateTime parsing)
        residuals = []
        for pick_idx, ev_idx, _score in assignments_list:
            pick = picks_df.iloc[pick_idx]
            sta_id = f"{pick.get('network', '')}.{pick.get('station', '')}"
            sta = station_lookup.get(sta_id)
            if sta is None:
                continue
            phase = pick.get("phase", pick.get("type", ""))
            pick_epoch = pd.Timestamp(pick.get("time", pick.get("timestamp", ""))).timestamp()
            observed_tt = pick_epoch - ev_epoch

            # Predicted travel time
            d_horiz = haversine_km(lat, lon, sta["latitude"], sta["longitude"])
            r = math.sqrt(d_horiz ** 2 + depth ** 2)
            vel = 5.0 if phase == "P" else 2.89
            predicted_tt = r / vel

            residuals.append({
                "station": pick.get("station", sta_id.split(".")[-1]),
                "phase": phase,
                "observed_s": round(observed_tt, 4),
                "predicted_s": round(predicted_tt, 4),
                "residual_s": round(observed_tt - predicted_tt, 4),
            })

        # Count picks used
        num_picks = len(assignments_list)

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Post-association processing failed: {exc}")

    return {
        "location": {
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "depth_km": round(depth, 2),
            "time": ev_time,
            "sigma_time": round(ev.get("sigma_time", 0.0), 4),
            "sigma_amp": round(ev.get("sigma_amp", 0.0), 4),
            "num_picks": num_picks,
        },
        "magnitude": {
            "ml": round(ml, 2) if ml is not None else None,
            "ml_err": round(ml_err, 2) if ml_err is not None else None,
            "num_ml_sta": ml_count,
        },
        "residuals": residuals,
    }


# ── POST /api/event/{event_id}/save ──────────────────────────────
@app.post("/api/event/{event_id}/save")
async def event_save(event_id: str, request: Request):
    """Save reviewed event to catalog and assignments."""
    body = await request.json()
    picks_input = body.get("picks", [])
    location = body.get("location", {})
    magnitude = body.get("magnitude", {})

    if not location:
        raise HTTPException(status_code=400, detail="Location data required")

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── Update catalog.csv ──
    catalog_rows = _read_catalog()
    found = False
    for row in catalog_rows:
        if row.get("event_id") == event_id:
            row["latitude"] = str(round(float(location["latitude"]), 6))
            row["longitude"] = str(round(float(location["longitude"]), 6))
            row["depth_km"] = str(round(float(location["depth_km"]), 2))
            row["time"] = location.get("time", row.get("time", ""))
            row["sigma_time"] = str(round(float(location.get("sigma_time", 0)), 4))
            row["sigma_amp"] = str(round(float(location.get("sigma_amp", 0)), 4))
            row["num_picks"] = str(location.get("num_picks", row.get("num_picks", 0)))
            if magnitude.get("ml") is not None:
                row["magnitude"] = str(round(float(magnitude["ml"]), 2))
                row["magnitude_type"] = "ML"
            if magnitude.get("ml_err") is not None:
                row["ml_err"] = str(round(float(magnitude["ml_err"]), 2))
            if magnitude.get("num_ml_sta") is not None:
                row["num_ml_sta"] = str(magnitude["num_ml_sta"])
            row["reviewed"] = now_utc
            row["review_status"] = body.get("review_status", "confirmed")
            row["event_type"] = body.get("event_type", row.get("event_type", "undetermined"))
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found in catalog")

    # Use all columns from the existing catalog, adding reviewed/review_status/event_type if not present
    if catalog_rows:
        catalog_fields = list(catalog_rows[0].keys())
        if "reviewed" not in catalog_fields:
            catalog_fields.append("reviewed")
        if "review_status" not in catalog_fields:
            catalog_fields.append("review_status")
        if "event_type" not in catalog_fields:
            catalog_fields.append("event_type")
    else:
        catalog_fields = [
            "event_id", "event_index", "time", "magnitude", "magnitude_type", "ml_err",
            "latitude", "longitude", "depth_km", "sigma_time", "sigma_amp",
            "num_picks", "num_ml_sta", "reviewed", "review_status", "event_type",
        ]
    _atomic_write_csv(CATALOG_FILE, catalog_rows, catalog_fields)

    # ── Update assignments.csv ──
    all_assignments = _read_assignments()
    # Remove old assignments for this event
    other_assignments = [a for a in all_assignments if a.get("event_id") != event_id]
    # Add new picks
    for p in picks_input:
        other_assignments.append({
            "event_id": event_id,
            "network": p.get("network", ""),
            "station": p.get("station", ""),
            "location": p.get("location", ""),
            "channel": p.get("channel", ""),
            "phase": p.get("phase", ""),
            "time": p.get("time", ""),
            "probability": str(p.get("probability", "")),
            "amplitude": str(p.get("amplitude", "")),
            "amplitude_channel": p.get("amplitude_channel", ""),
        })

    assign_fields = [
        "event_id", "network", "station", "location", "channel",
        "phase", "time", "probability", "amplitude", "amplitude_channel",
    ]
    _atomic_write_csv(ASSIGNMENTS_FILE, other_assignments, assign_fields)

    # Invalidate dashboard caches
    for key in list(_cache.keys()):
        del _cache[key]

    # Return updated event
    for row in catalog_rows:
        if row.get("event_id") == event_id:
            return {"event": _parse_catalog_row(row), "saved": True}

    return {"saved": True}


# ── POST /api/event/{event_id}/review-status ─────────────────────
@app.post("/api/event/{event_id}/review-status")
async def event_review_status(event_id: str, request: Request):
    """Quick status-only update (confirm/reject without relocation)."""
    body = await request.json()
    status = body.get("status", "")
    if status not in ("confirmed", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'confirmed' or 'rejected'")

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    catalog_rows = _read_catalog()
    found = False
    for row in catalog_rows:
        if row.get("event_id") == event_id:
            row["review_status"] = status
            row["reviewed"] = now_utc
            if "event_type" in body:
                row["event_type"] = body["event_type"]
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found in catalog")

    if catalog_rows:
        catalog_fields = list(catalog_rows[0].keys())
        if "reviewed" not in catalog_fields:
            catalog_fields.append("reviewed")
        if "review_status" not in catalog_fields:
            catalog_fields.append("review_status")
        if "event_type" not in catalog_fields:
            catalog_fields.append("event_type")
    else:
        catalog_fields = [
            "event_id", "event_index", "time", "magnitude", "magnitude_type", "ml_err",
            "latitude", "longitude", "depth_km", "sigma_time", "sigma_amp",
            "num_picks", "num_ml_sta", "reviewed", "review_status", "event_type",
        ]
    _atomic_write_csv(CATALOG_FILE, catalog_rows, catalog_fields)

    # Invalidate dashboard caches
    for key in list(_cache.keys()):
        del _cache[key]

    for row in catalog_rows:
        if row.get("event_id") == event_id:
            return {"event": _parse_catalog_row(row), "updated": True}

    return {"updated": True}
