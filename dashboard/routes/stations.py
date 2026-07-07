"""Station list + per-station state-of-health detail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from dashboard.deps import RAW_DIR, load_stations

router = APIRouter()

# How many recent calendar days the detail endpoint summarizes.
DETAIL_DAYS = 14


@router.get("/api/stations")
async def stations():
    return load_stations()


@router.get("/api/station/{station}/detail")
async def station_detail(station: str):
    """State-of-health for one station: recent per-day raw-file coverage
    and the end time of its newest data.

    Day-batch context matters for interpretation: with the pipeline's
    ~30 h ingest lag, "newest data ends ~a day ago" is HEALTHY. The
    latency threshold coloring is applied client-side for that reason.
    """
    meta = next((s for s in load_stations() if s["station"] == station), None)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Unknown station: {station}")

    today = datetime.now(timezone.utc).date()
    days = []
    newest_file = None
    for offset in range(DETAIL_DAYS - 1, -1, -1):
        d = today - timedelta(days=offset)
        day_dir = RAW_DIR / str(d.year) / f"{d.timetuple().tm_yday:03d}"
        files = sorted(day_dir.glob(f"*.{station}.*.mseed")) if day_dir.is_dir() else []
        days.append({"date": d.isoformat(), "files": len(files)})
        if files:
            newest_file = files[-1]

    last_data_utc = None
    latency_hours = None
    if newest_file is not None:
        try:
            from obspy import read

            st = read(str(newest_file), headonly=True)
            end = max(tr.stats.endtime for tr in st)
            last_data_utc = str(end)
            latency_hours = round(
                (datetime.now(timezone.utc) - end.datetime.replace(tzinfo=timezone.utc)).total_seconds()
                / 3600.0,
                1,
            )
        except Exception:
            # Corrupt/locked file: fall back to the file's mtime.
            mtime = datetime.fromtimestamp(newest_file.stat().st_mtime, tz=timezone.utc)
            last_data_utc = mtime.isoformat(timespec="seconds")
            latency_hours = round((datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0, 1)

    covered = sum(1 for d in days if d["files"] > 0)
    return {
        "station": meta["station"],
        "network": meta["network"],
        "model": meta.get("model", ""),
        "channels": meta.get("channels", ""),
        "latitude": meta["latitude"],
        "longitude": meta["longitude"],
        "elevation_m": meta.get("elevation_m"),
        "last_data_utc": last_data_utc,
        "latency_hours": latency_hours,
        "days": days,
        "days_covered": covered,
        "days_window": DETAIL_DAYS,
    }
