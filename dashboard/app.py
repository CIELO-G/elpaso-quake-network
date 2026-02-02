"""FastAPI application — serves dashboard UI and pipeline status API."""

import csv
import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

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

app = FastAPI(title="El Paso Seismic Pipeline Dashboard")

# Cache stations in memory (loaded once)
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


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/status")
async def status():
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text())
    return {"pipeline": {"status": "idle"}, "steps": []}


@app.get("/api/logs/{step_name}", response_class=PlainTextResponse)
async def logs(step_name: str, lines: int = 100):
    if step_name not in VALID_STEP_NAMES:
        return PlainTextResponse(
            f"Unknown step: {step_name}", status_code=400
        )
    log_path = LOGS_DIR / f"{step_name}.log"
    if not log_path.exists():
        return PlainTextResponse(f"No log file for {step_name}")
    all_lines = log_path.read_text().splitlines()
    tail = all_lines[-lines:]
    return "\n".join(tail) + "\n"


@app.get("/api/stations")
async def stations():
    return _load_stations()


@app.get("/api/catalog")
async def catalog():
    rows = _read_catalog()
    events = []
    for r in rows:
        events.append({
            "event_id": r.get("event_id", ""),
            "time": r.get("time", ""),
            "magnitude": float(r["magnitude"]) if r.get("magnitude") else None,
            "latitude": float(r["latitude"]) if r.get("latitude") else None,
            "longitude": float(r["longitude"]) if r.get("longitude") else None,
            "depth_km": float(r["depth_km"]) if r.get("depth_km") else None,
            "num_picks": int(r["num_picks"]) if r.get("num_picks") else None,
        })
    return {"events": events}


@app.get("/api/stats")
async def stats():
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
    days_with_picks = len(pick_files)
    for pf in pick_files:
        try:
            # subtract 1 for header line
            total_picks += sum(1 for _ in open(pf)) - 1
        except OSError:
            pass

    # Count event day files
    event_files = list(EVENTS_DIR.rglob("*.events.csv"))
    days_with_events = len(event_files)

    # Count raw day directories
    days_with_raw = 0
    if RAW_DIR.exists():
        for year_dir in RAW_DIR.iterdir():
            if year_dir.is_dir():
                days_with_raw += sum(
                    1 for d in year_dir.iterdir() if d.is_dir()
                )

    station_count = len(_load_stations())

    return {
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


@app.get("/api/disk")
async def disk():
    usage = shutil.disk_usage(ROOT)
    total_gb = round(usage.total / (1024 ** 3), 1)
    used_gb = round(usage.used / (1024 ** 3), 1)
    free_gb = round(usage.free / (1024 ** 3), 1)
    usage_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0

    # Per-step breakdown
    step_dirs = ["1-raw", "1-metadata", "2-processed", "3-picks", "4-events", "5-catalog"]
    breakdown = {}
    output_total = 0
    for d in step_dirs:
        size = _dir_size(OUTPUT_DIR / d)
        breakdown[d] = round(size / (1024 ** 3), 3)
        output_total += size

    # Include the downloads database
    db_file = OUTPUT_DIR / "1-downloads.db"
    if db_file.exists():
        db_size = db_file.stat().st_size
        breakdown["1-downloads.db"] = round(db_size / (1024 ** 3), 3)
        output_total += db_size

    return {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "usage_percent": usage_percent,
        "output_size_gb": round(output_total / (1024 ** 3), 2),
        "breakdown": breakdown,
    }


def _count_day_dirs(base: Path) -> int:
    """Count year/doy day directories under a step output dir."""
    count = 0
    if not base.exists():
        return 0
    for year_dir in base.iterdir():
        if year_dir.is_dir() and year_dir.name.isdigit():
            count += sum(1 for d in year_dir.iterdir() if d.is_dir())
    return count


@app.get("/api/progress")
async def progress():
    target_date = (
        datetime.now(timezone.utc) - timedelta(hours=LAG_HOURS)
    ).date()
    total_days = max((target_date - PIPELINE_START_DATE).days + 1, 0)

    days_ingested = _count_day_dirs(RAW_DIR)
    days_processed = _count_day_dirs(PROCESSED_DIR)
    days_detected = _count_day_dirs(PICKS_DIR)
    days_associated = _count_day_dirs(EVENTS_DIR)

    # Read pipeline_status.json for current state
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

    # The furthest completed step determines overall progress
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


# Regex for standard Python logging lines: "YYYY-MM-DD HH:MM:SS LEVEL  msg"
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(ERROR|CRITICAL|WARNING)\s+(.+)$"
)


@app.get("/api/errors")
async def errors(limit: int = 50):
    entries: list[dict] = []

    # Scan each log file for ERROR/CRITICAL/WARNING lines
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

    # Also flag any currently-failed steps from pipeline status
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

    # Sort by timestamp descending, return most recent
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"errors": entries[:limit]}


@app.get("/api/throughput")
async def throughput():
    """Per-day timing averages and ETA, sourced from continuous-mode status."""
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

    # Per-step averages
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

    # ETA based on remaining days
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
async def station_health():
    """Check which stations have raw data in the most recent day directories."""
    stations = _load_stations()

    # Collect the latest 3 day directories from 1-raw/
    day_dirs: list[Path] = []
    if RAW_DIR.exists():
        for year_dir in sorted(RAW_DIR.iterdir(), reverse=True):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for doy_dir in sorted(year_dir.iterdir(), reverse=True):
                if not doy_dir.is_dir() or not doy_dir.name.isdigit():
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
            for s in stations
        ]

    results = []
    for s in stations:
        prefix = f"{s['network']}.{s['station']}."
        found = 0
        for d in day_dirs:
            if any(d.glob(f"{prefix}*.mseed")):
                found += 1
        if found == len(day_dirs):
            status = "ok"
        elif found > 0:
            status = "warning"
        else:
            status = "error"
        results.append({
            "station": s["station"],
            "network": s["network"],
            "status": status,
            "files_recent": found,
        })
    return results


@app.get("/api/event_rate")
async def event_rate():
    """Events per day from the catalog, for the timeline chart."""
    rows = _read_catalog()
    counts: dict[str, int] = {}
    for r in rows:
        t = r.get("time", "")
        if len(t) >= 10:
            day = t[:10]
            counts[day] = counts.get(day, 0) + 1
    days = [{"date": d, "count": c} for d, c in sorted(counts.items())]
    return {"days": days}


@app.get("/api/station_picks")
async def station_picks():
    """Picks per station per day, for the activity heatmap."""
    stations = _load_stations()
    station_names = [s["station"] for s in stations]

    counts: dict[str, dict[str, int]] = {}  # station -> {date -> count}
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
