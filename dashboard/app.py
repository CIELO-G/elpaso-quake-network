"""FastAPI application — serves dashboard UI and pipeline status API."""

import csv
import hashlib
import json
import os
import re
import secrets
import shutil
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

app = FastAPI(title="El Paso Seismic Pipeline Dashboard")

# ── CORS ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
        return JSONResponse(status_code=304, content=None, headers={"ETag": etag})
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


def _parse_catalog_row(r: dict) -> dict:
    return {
        "event_id": r.get("event_id", ""),
        "time": r.get("time", ""),
        "magnitude": float(r["magnitude"]) if r.get("magnitude") else None,
        "latitude": float(r["latitude"]) if r.get("latitude") else None,
        "longitude": float(r["longitude"]) if r.get("longitude") else None,
        "depth_km": float(r["depth_km"]) if r.get("depth_km") else None,
        "num_picks": int(r["num_picks"]) if r.get("num_picks") else None,
    }


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
    _ws_clients -= dead


# ── Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


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
    if rows:
        latest = rows[-1]
        latest_event_time = latest.get("time")
        latest_event_id = latest.get("event_id")

    # Count picks
    total_picks = 0
    days_with_picks = 0
    pick_files = list(PICKS_DIR.rglob("*.picks.csv"))
    for pf in pick_files:
        try:
            count = sum(1 for _ in open(pf)) - 1
            total_picks += count
            if count > 0:
                days_with_picks += 1
        except OSError:
            pass

    # Count event day files
    days_with_events = 0
    for ef in EVENTS_DIR.rglob("*.events.csv"):
        try:
            if sum(1 for _ in open(ef)) > 1:
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


def _count_days_with_data(base: Path, glob_pattern: str) -> int:
    count = 0
    for csv_file in base.rglob(glob_pattern):
        try:
            if sum(1 for _ in open(csv_file)) > 1:
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

    current_day = None
    pipeline_status = None
    mode = "single"
    waiting = False
    next_run_at = None
    days_completed_cont = None
    days_skipped = []

    if STATUS_FILE.exists():
        try:
            status_data = json.loads(STATUS_FILE.read_text())
            pipeline_status = status_data.get("pipeline", {}).get("status")
            mode = status_data.get("pipeline", {}).get("mode", "single")
            args = status_data.get("pipeline", {}).get("args", {})
            current_day = args.get("start") or args.get("end")

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
    }


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

    # Find picks for this event's date
    event_picks = []
    t = event_row.get("time", "")
    if len(t) >= 10:
        event_date = t[:10]
        # Search pick files for matching picks
        for pf in PICKS_DIR.rglob("*.picks.csv"):
            try:
                with open(pf, newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        pick_time = row.get("time", "")
                        if pick_time.startswith(event_date):
                            event_picks.append({
                                "station": row.get("station", ""),
                                "phase": row.get("phase", ""),
                                "time": pick_time,
                                "probability": float(row.get("probability", 0)) if row.get("probability") else None,
                            })
            except (OSError, ValueError):
                pass

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
    fieldnames = ["event_id", "time", "magnitude", "latitude", "longitude", "depth_km", "num_picks"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})

    return PlainTextResponse(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=catalog_export.csv"},
    )
