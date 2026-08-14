"""Catalog & statistics endpoints.

Covers the paginated catalog browser, summary stats, single-event detail,
event-rate time series, and the Gutenberg-Richter / depth distribution
science views.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Query, Request

from dashboard.cache import etag_response, get_cached, set_cached
from dashboard.deps import (
    CATALOG_FILE,
    EVENTS_DIR,
    PICKS_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    filter_by_date,
    load_stations,
    parse_catalog_row,
    read_assignments,
    read_catalog,
)

router = APIRouter()


# ── Catalog browser (paginated, filterable, sortable) ────────────
@router.get("/api/catalog")
async def catalog(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    sort_by: str | None = Query(default=None, pattern=r"^(time|magnitude|depth_km|num_picks)$"),
    sort_order: str | None = Query(default="desc", pattern=r"^(asc|desc)$"),
    min_magnitude: float | None = Query(default=None),
    max_depth: float | None = Query(default=None),
    min_picks: int | None = Query(default=None),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    review_status: str | None = Query(default=None, pattern=r"^(unreviewed|confirmed|rejected)$"),
):
    entry = get_cached("catalog", ttl=30.0, watch_file=CATALOG_FILE)
    if entry:
        all_events = entry.data
    else:
        rows = read_catalog()
        all_events = [parse_catalog_row(r) for r in rows]
        set_cached("catalog", all_events, watch_file=CATALOG_FILE)

    if start_date or end_date:
        all_events = filter_by_date(all_events, start_date, end_date)

    if min_magnitude is not None:
        all_events = [
            e for e in all_events if e["magnitude"] is not None and e["magnitude"] >= min_magnitude
        ]
    if max_depth is not None:
        all_events = [
            e for e in all_events if e["depth_km"] is not None and e["depth_km"] <= max_depth
        ]
    if min_picks is not None:
        all_events = [
            e for e in all_events if e["num_picks"] is not None and e["num_picks"] >= min_picks
        ]
    if search:
        s = search.lower()
        all_events = [
            e
            for e in all_events
            if s in (e.get("event_id") or "").lower() or s in (e.get("time") or "").lower()
        ]
    if review_status:
        if review_status == "unreviewed":
            all_events = [e for e in all_events if not e.get("review_status")]
        else:
            all_events = [e for e in all_events if e.get("review_status") == review_status]

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

    return {
        "events": all_events[start_idx:end_idx],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


# ── Summary stats (hero cards) ──────────────────────────────────
@router.get("/api/stats")
async def stats(request: Request):
    entry = get_cached("stats", ttl=30.0, watch_file=CATALOG_FILE)
    if entry:
        return etag_response(entry.data, entry.etag, request)

    rows = read_catalog()
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

    total_picks = 0
    days_with_picks = 0
    for pf in PICKS_DIR.rglob("*.picks.csv"):
        try:
            with open(pf) as fh:
                count = sum(1 for _ in fh) - 1
            total_picks += count
            if count > 0:
                days_with_picks += 1
        except OSError:
            pass

    days_with_events = 0
    for ef in EVENTS_DIR.rglob("*.events.csv"):
        try:
            with open(ef) as fh:
                if sum(1 for _ in fh) > 1:
                    days_with_events += 1
        except OSError:
            pass

    # Raw ∪ processed: local raw is pruned after archiving, but processed
    # day files prove the day's data existed.
    waveform_days: set[tuple[str, str]] = set()
    for base in (RAW_DIR, PROCESSED_DIR):
        if not base.exists():
            continue
        for year_dir in base.iterdir():
            if year_dir.is_dir():
                for d in year_dir.iterdir():
                    key = (year_dir.name, d.name)
                    if key in waveform_days or not d.is_dir():
                        continue
                    if sum(1 for _ in d.glob("*.mseed")) >= 3:
                        waveform_days.add(key)
    days_with_raw = len(waveform_days)

    station_count = len(load_stations())

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
    etag = set_cached("stats", data, watch_file=CATALOG_FILE)
    return etag_response(data, etag, request)


# ── Single-event detail ──────────────────────────────────────────
@router.get("/api/event/{event_id}")
async def event_detail(event_id: str):
    """Single event + its picks + per-station phase counts."""
    rows = read_catalog()
    event_row = next((r for r in rows if r.get("event_id") == event_id), None)
    if event_row is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    event = parse_catalog_row(event_row)

    event_picks = []
    for a in read_assignments():
        if a.get("event_id") == event_id:
            event_picks.append(
                {
                    "network": a.get("network", ""),
                    "station": a.get("station", ""),
                    "location": a.get("location", ""),
                    "channel": a.get("channel", ""),
                    "phase": a.get("phase", ""),
                    "time": a.get("time", ""),
                    "probability": float(a["probability"]) if a.get("probability") else None,
                    "amplitude": float(a["amplitude"]) if a.get("amplitude") else None,
                }
            )

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


# ── Events per day (timeline chart) ──────────────────────────────
@router.get("/api/event_rate")
async def event_rate(
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    rows = read_catalog()
    if start_date or end_date:
        rows = filter_by_date(rows, start_date, end_date)
    counts: dict[str, int] = {}
    for r in rows:
        t = r.get("time", "")
        if len(t) >= 10:
            day = t[:10]
            counts[day] = counts.get(day, 0) + 1
    return {"days": [{"date": d, "count": c} for d, c in sorted(counts.items())]}


# ── Gutenberg-Richter magnitude-frequency ────────────────────────
@router.get("/api/magnitude_frequency")
async def magnitude_frequency(
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    bin_width: float = Query(default=0.1, ge=0.01, le=1.0),
):
    """Magnitude-frequency distribution + b-value via least squares on M ≥ Mc."""
    rows = read_catalog()
    if start_date or end_date:
        rows = filter_by_date(rows, start_date, end_date)

    magnitudes = [float(r["magnitude"]) for r in rows if r.get("magnitude")]
    if len(magnitudes) < 2:
        return {
            "bins": [],
            "cumulative": [],
            "b_value": None,
            "a_value": None,
            "mc": None,
            "r_squared": None,
            "total_events": len(magnitudes),
        }

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
    if magnitudes:
        counts[-1] = sum(1 for m in magnitudes if m >= round(mag_min + (n_bins - 1) * bin_width, 4))

    cumulative = []
    running = 0
    for i in range(len(counts) - 1, -1, -1):
        running += counts[i]
        cumulative.append(running)
    cumulative.reverse()

    log10_n = [round(math.log10(c), 4) if c > 0 else None for c in cumulative]

    # Mc: bin with the largest non-cumulative count (maximum curvature).
    mc_idx = counts.index(max(counts))
    mc = bins[mc_idx]

    # b-value via least squares on M ≥ Mc.
    fit_m = []
    fit_logn = []
    for i, b in enumerate(bins):
        if b >= mc and cumulative[i] > 0:
            fit_m.append(b)
            fit_logn.append(math.log10(cumulative[i]))

    b_value = a_value = r_squared = None
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


# ── Depth distribution ──────────────────────────────────────────
@router.get("/api/depth_distribution")
async def depth_distribution(
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    bin_size: float = Query(default=1.0, ge=0.1, le=10.0),
):
    """Depth histogram + scatter (for cross-section plots)."""
    rows = read_catalog()
    if start_date or end_date:
        rows = filter_by_date(rows, start_date, end_date)

    events = [parse_catalog_row(r) for r in rows]
    events = [e for e in events if e["depth_km"] is not None]

    if not events:
        return {
            "histogram": {"bins": [], "counts": []},
            "median_depth": None,
            "mean_depth": None,
            "scatter": [],
        }

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
        for e in events
        if e["latitude"] is not None and e["longitude"] is not None
    ]

    return {
        "histogram": {"bins": hist_bins, "counts": hist_counts, "bin_size": bin_size},
        "median_depth": median_depth,
        "mean_depth": mean_depth,
        "scatter": scatter,
    }
