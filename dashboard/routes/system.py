"""System & monitoring endpoints: index page, health, status, logs, disk, errors.

This module owns the WebSocket status fan-out (`_ws_clients` + `broadcast`).
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from dashboard.cache import etag_response, get_cached, set_cached
from dashboard.deps import (
    CATALOG_FILE,
    FAULTS_FILE,
    LOG_LINE_RE,
    LOGS_DIR,
    MAX_LOG_BYTES,
    OUTPUT_DIR,
    ROOT,
    STATIC_DIR,
    STATUS_FILE,
    VALID_STEP_NAMES,
    dir_size,
    strip_ansi,
)

router = APIRouter()

# Process-wide app start time for the health uptime field.
_app_start_time = time.monotonic()


# ── HTML index + static asset ────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@router.get("/api/faults")
async def faults():
    if not FAULTS_FILE.exists():
        return JSONResponse({"type": "FeatureCollection", "features": []})
    return FileResponse(FAULTS_FILE, media_type="application/geo+json")


# ── Health ───────────────────────────────────────────────────────
@router.get("/api/health")
async def health():
    """Structured health check (status, last run, catalog size, disk, uptime)."""
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
                catalog_size = max(sum(1 for _ in f) - 1, 0)
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


# ── Pipeline status (cached + WS broadcast) ──────────────────────
_ws_clients: set[WebSocket] = set()


@router.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        # Send initial state on connect
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                await websocket.send_json(data)
            except (json.JSONDecodeError, OSError):
                pass
        # Keep socket alive; client may send pings, we watch for disconnect
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        _ws_clients.discard(websocket)


async def _broadcast_status() -> None:
    """Push current status to all connected WebSocket clients."""
    if not _ws_clients or not STATUS_FILE.exists():
        return
    try:
        data = json.loads(STATUS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    dead: set[WebSocket] = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


@router.get("/api/status")
async def api_status(request: Request):
    entry = get_cached("status", ttl=2.0, watch_file=STATUS_FILE)
    if entry:
        return etag_response(entry.data, entry.etag, request)
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            raise HTTPException(status_code=500, detail="Failed to read pipeline status file")
    else:
        data = {"pipeline": {"status": "idle"}, "steps": []}
    etag = set_cached("status", data, watch_file=STATUS_FILE)
    await _broadcast_status()
    return etag_response(data, etag, request)


# ── Per-step log tail ────────────────────────────────────────────
@router.get("/api/logs/{step_name}", response_class=PlainTextResponse)
async def logs(step_name: str, lines: int = Query(default=100, ge=1, le=5000)):
    if step_name not in VALID_STEP_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown step: {step_name}. Valid: {', '.join(sorted(VALID_STEP_NAMES))}",
        )
    log_path = LOGS_DIR / f"{step_name}.log"
    if not log_path.exists():
        return PlainTextResponse(f"No log file for {step_name}")
    try:
        all_lines = log_path.read_text().splitlines()
    except OSError:
        raise HTTPException(status_code=500, detail=f"Failed to read log file for {step_name}")
    tail = all_lines[-lines:]
    cleaned = [strip_ansi(line) for line in tail]
    output = "\n".join(cleaned) + "\n"
    if len(output.encode("utf-8")) > MAX_LOG_BYTES:
        output_bytes = output.encode("utf-8")[:MAX_LOG_BYTES]
        output = output_bytes.decode("utf-8", errors="ignore") + "\n... [truncated to 100KB]\n"
    return output


# ── Disk usage ───────────────────────────────────────────────────
@router.get("/api/disk")
async def disk(request: Request):
    entry = get_cached("disk", ttl=60.0)
    if entry:
        return etag_response(entry.data, entry.etag, request)

    usage = shutil.disk_usage(ROOT)
    total_gb = round(usage.total / (1024 ** 3), 1)
    used_gb = round(usage.used / (1024 ** 3), 1)
    free_gb = round(usage.free / (1024 ** 3), 1)
    usage_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0

    step_dirs = ["1-raw", "1-metadata", "2-processed", "3-picks", "4-events", "5-catalog"]
    breakdown = {}
    output_total = 0
    for d in step_dirs:
        size = dir_size(OUTPUT_DIR / d)
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
    etag = set_cached("disk", data)
    return etag_response(data, etag, request)


# ── Error log aggregation ────────────────────────────────────────
@router.get("/api/errors")
async def errors(limit: int = Query(default=50, ge=1, le=500)):
    entries: list[dict] = []

    for step_name in ("ingest", "process", "detect", "associate", "catalog"):
        log_path = LOGS_DIR / f"{step_name}.log"
        if not log_path.exists():
            continue
        try:
            for line in log_path.read_text().splitlines():
                m = LOG_LINE_RE.match(line)
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
