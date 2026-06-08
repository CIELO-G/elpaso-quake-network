"""Pipeline lifecycle + progress / throughput / completeness endpoints.

Owns the ``_pipeline_proc`` subprocess handle and the helpers that walk
the output tree to report progress per stage.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from dashboard.cache import etag_response, get_cached, set_cached
from dashboard.deps import (
    CATALOG_FILE,
    EVENTS_DIR,
    LAG_HOURS,
    LOGS_DIR,
    PICKS_DIR,
    PIPELINE_PID_FILE,
    PIPELINE_SCRIPT,
    PIPELINE_START_DATE,
    PROCESSED_DIR,
    RAW_DIR,
    STATUS_FILE,
    load_stations,
)

router = APIRouter()

# Subprocess handle for the running pipeline (may be reattached to a PID file
# orphan after a dashboard restart — see _is_pipeline_running).
_pipeline_proc: Optional[subprocess.Popen] = None


# ── Tree-walking helpers (used by progress) ──────────────────────
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
    """Latest YYYY-MM-DD string from a year/jday directory tree."""
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
    """Latest YYYY-MM-DD string from year/jday CSV files."""
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


# ── Progress ─────────────────────────────────────────────────────
@router.get("/api/progress")
async def progress(request: Request):
    # Cache: walks 4 directory trees on every call, called every 10s by
    # pollData. 5s TTL + STATUS_FILE invalidation = at most 1 walk per pipeline
    # step transition, free hits the rest of the time.
    entry = get_cached("progress", ttl=5.0, watch_file=STATUS_FILE)
    if entry:
        return etag_response(entry.data, entry.etag, request)
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

    data = {
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
    etag = set_cached("progress", data, watch_file=STATUS_FILE)
    return etag_response(data, etag, request)


# ── Throughput / ETA ─────────────────────────────────────────────
@router.get("/api/throughput")
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


# ── Pipeline lifecycle (start / stop / running) ──────────────────
def _is_pipeline_running() -> tuple[bool, int | None]:
    """Whether the pipeline subprocess is running.

    First checks the in-memory subprocess handle; falls back to the PID file
    so a dashboard restart can still detect an orphaned pipeline.
    """
    global _pipeline_proc

    if _pipeline_proc is not None:
        if _pipeline_proc.poll() is None:
            return True, _pipeline_proc.pid
        _pipeline_proc = None
        PIPELINE_PID_FILE.unlink(missing_ok=True)

    if PIPELINE_PID_FILE.exists():
        try:
            pid = int(PIPELINE_PID_FILE.read_text().strip())
            os.kill(pid, 0)  # signal-0 probe — doesn't actually send a signal
            return True, pid
        except (ValueError, ProcessLookupError, PermissionError):
            PIPELINE_PID_FILE.unlink(missing_ok=True)

    return False, None


@router.get("/api/pipeline/running")
async def pipeline_running():
    running, pid = _is_pipeline_running()
    return {"running": running, "pid": pid}


@router.post("/api/pipeline/start")
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
        start = body.get("start")
        end = body.get("end")
        if not start or not end:
            raise HTTPException(status_code=400, detail="backfill mode requires start and end dates")
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid date format (use YYYY-MM-DD)")
        cmd += ["--start", start, "--end", end]

    if body.get("force"):
        cmd.append("--force")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(LOGS_DIR / "pipeline_stdout.log", "a")

    _pipeline_proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    PIPELINE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIPELINE_PID_FILE.write_text(str(_pipeline_proc.pid))

    return {"started": True, "pid": _pipeline_proc.pid, "mode": mode}


@router.post("/api/pipeline/stop")
async def pipeline_stop():
    global _pipeline_proc

    running, pid = _is_pipeline_running()
    if not running or pid is None:
        raise HTTPException(status_code=404, detail="No pipeline process is running")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    _pipeline_proc = None
    PIPELINE_PID_FILE.unlink(missing_ok=True)

    return {"stopped": True, "pid": pid}


# ── Per-station health (last 3 days of raw data coverage) ───────
@router.get("/api/station_health")
async def station_health():
    stations_data = load_stations()

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


# ── Raw data completeness matrix (calendar heatmap) ──────────────
@router.get("/api/data_completeness")
async def data_completeness(request: Request):
    # Walks year/jday/file in RAW_DIR — at 14 stations × 365 days × 3 chan
    # = ~15k iterdir() calls. Cache 60s; new mseeds only land at ingest cadence.
    entry = get_cached("data_completeness", ttl=60.0)
    if entry:
        return etag_response(entry.data, entry.etag, request)
    stations_data = load_stations()
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

    data = {"stations": station_names, "days": all_days, "matrix": matrix}
    etag = set_cached("data_completeness", data)
    return etag_response(data, etag, request)


# ── Pick probability distribution ────────────────────────────────
@router.get("/api/pick_quality")
async def pick_quality(request: Request):
    # Walks + parses every picks CSV. Heavy. Cache 120s; new picks only
    # appear at detect cadence (per-day, hours apart).
    entry = get_cached("pick_quality", ttl=120.0)
    if entry:
        return etag_response(entry.data, entry.etag, request)
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

    data = {
        "bins": bins,
        "bin_edges": [round(i * 0.1, 1) for i in range(11)],
        "total": total,
        "p_count": p_count,
        "s_count": s_count,
    }
    etag = set_cached("pick_quality", data)
    return etag_response(data, etag, request)


# ── Per-station daily pick counts (activity heatmap) ─────────────
@router.get("/api/station_picks")
async def station_picks(request: Request):
    # Walks + parses every picks CSV. Heavy. Cache 120s (matches pick_quality).
    entry = get_cached("station_picks", ttl=120.0)
    if entry:
        return etag_response(entry.data, entry.etag, request)
    stations_data = load_stations()
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

    data = {"stations": station_names, "days": all_days, "matrix": matrix}
    etag = set_cached("station_picks", data)
    return etag_response(data, etag, request)
