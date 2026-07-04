"""Event review endpoints: relocate (grid-search), save, status-only update.

Uses the 1D-layered grid-search locator from ``lib.location`` for the
relocate flow. Saves are written atomically (temp file + ``os.replace``)
and invalidate the in-process caches so the catalog view picks up the
update on the very next request.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from dashboard.cache import clear_all as clear_caches
from dashboard.deps import (
    ASSIGNMENTS_FILE,
    CATALOG_FILE,
    atomic_write_csv,
    parse_catalog_row,
    read_assignments,
    read_catalog,
)
from dashboard.locators import init_nlloc_locator

router = APIRouter()

# Serializes catalog/assignments read-modify-write across concurrent reviewers.
# Without this, two reviewers saving simultaneously (3-user Tailscale deploy)
# would both read the same starting state and the second write would silently
# overwrite the first. asyncio.Lock is sufficient because uvicorn runs as a
# single-process single-worker — if we ever scale to multi-worker uvicorn this
# needs to become an fcntl.flock on a sentinel file.
_catalog_write_lock = asyncio.Lock()

# Near-duplicate detection thresholds for the save-time warning (matches
# scripts/dedup_catalog.py defaults).
_DUP_DT_S = 2.0
_DUP_DIST_KM = 5.0


def _near_duplicates(catalog_rows: list[dict], event_id: str, time_iso: str,
                     lat: float, lon: float) -> list[str]:
    """Event IDs of OTHER catalog events within the duplicate thresholds."""
    def _parse(iso: str) -> datetime:
        # Catalog times mix 'Z'-suffixed and naive UTC strings.
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t

    try:
        t0 = _parse(time_iso)
    except (ValueError, AttributeError):
        return []
    dupes = []
    for row in catalog_rows:
        if row.get("event_id") == event_id:
            continue
        try:
            t1 = _parse(row["time"])
            if abs((t1 - t0).total_seconds()) > _DUP_DT_S:
                continue
            row_lat = float(row["latitude"])
            dlat = (row_lat - lat) * 111.19
            # Midpoint latitude — matches scripts/dedup_catalog.py exactly
            dlon = (float(row["longitude"]) - lon) * 111.19 * math.cos(
                math.radians((row_lat + lat) / 2)
            )
            if math.hypot(dlat, dlon) <= _DUP_DIST_KM:
                dupes.append(row["event_id"])
        except (ValueError, KeyError, TypeError):
            continue
    return dupes

# Fallback columns when the catalog is being created from empty for the
# first time (otherwise we inherit the columns from existing rows).
_FALLBACK_CATALOG_COLUMNS = [
    "event_id",
    "event_index",
    "time",
    "magnitude",
    "magnitude_type",
    "ml_err",
    "latitude",
    "longitude",
    "depth_km",
    "sigma_time",
    "sigma_amp",
    "num_picks",
    "num_ml_sta",
    "reviewed",
    "review_status",
    "event_type",
]
_ASSIGNMENT_COLUMNS = [
    "event_id",
    "network",
    "station",
    "location",
    "channel",
    "phase",
    "time",
    "probability",
    "amplitude",
    "amplitude_channel",
]


def _catalog_columns_from_rows(rows: list[dict]) -> list[str]:
    """Use existing column order if available, else fallback."""
    if not rows:
        return list(_FALLBACK_CATALOG_COLUMNS)
    cols = list(rows[0].keys())
    for extra in ("reviewed", "review_status", "event_type"):
        if extra not in cols:
            cols.append(extra)
    return cols


# ── Relocate (NonLinLoc) ─────────────────────────────────────────
@router.post("/api/event/{event_id}/relocate")
async def event_relocate(event_id: str, request: Request):
    """Relocate an event from user-edited picks using NonLinLoc.

    The legacy ``init_grid_search_locator()`` path remains importable for
    comparison and fallback (see ``scripts/validate_locator.py``), but the
    dashboard endpoint uses NLLoc by default for publication-grade locations.

    Response shape mirrors the previous GaMMA / GridSearch contract
    (``location``, ``magnitude``, ``residuals``) with the same
    probabilistic uncertainty fields (``sigma_depth_km``,
    ``ellipse_major_km``, ``ellipse_minor_km``, ``ellipse_azimuth_deg``).
    """
    body = await request.json()
    picks_input = body.get("picks", [])
    if len(picks_input) < 4:
        raise HTTPException(status_code=400, detail="Need at least 4 picks for relocation")

    import pandas as pd

    from lib.location import Pick
    from lib.magnitude import (
        MLConfig,
        compute_ml_network,
        compute_ml_station,
        haversine_km,
    )

    locator = init_nlloc_locator()

    # Build Pick objects with times relative to the earliest pick (numerical
    # stability — NLLoc rebases internally but we still want small numbers in
    # any downstream RMS calc). Per-pick amplitudes kept separately for ML.
    try:
        ref_epoch = min(pd.Timestamp(p["time"]).timestamp() for p in picks_input)
        picks: list[Pick] = []
        pick_meta: list[dict] = []  # parallel list: station_id, amplitude, phase
        for p in picks_input:
            sta_id = f"{p['network']}.{p['station']}"
            t_rel = pd.Timestamp(p["time"]).timestamp() - ref_epoch
            picks.append(Pick(station_id=sta_id, phase=p["phase"], time_s=float(t_rel)))
            raw_amp = p.get("amplitude")
            amp_val = float(raw_amp) if raw_amp and float(raw_amp) > 0 else None
            pick_meta.append(
                {
                    "station_id": sta_id,
                    "station": p["station"],
                    "phase": p["phase"],
                    "amplitude": amp_val,
                    "time_s": float(t_rel),
                }
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid pick data: {exc}")

    # NLLoc precomputes its own travel-time grid; the grid_kwargs are
    # accepted and ignored by the wrapper for API parity.
    try:
        result = locator.locate(
            picks,
            ref_epoch_unix=ref_epoch,
            min_picks=4,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=f"Relocation failed: {exc}")
    except FileNotFoundError as exc:
        # NLLoc binary or travel-time grids missing — give a setup hint.
        raise HTTPException(
            status_code=500,
            detail=(
                f"NonLinLoc not ready: {exc}. Run scripts/nlloc_build_grids.py "
                "or set NLLOC_BIN_DIR."
            ),
        )
    except Exception as exc:  # pragma: no cover — defensive
        raise HTTPException(status_code=500, detail=f"Relocation failed: {exc}")

    lat = float(result.latitude)
    lon = float(result.longitude)
    depth = float(result.depth_km)

    # Absolute UTC origin time = ref_epoch + origin_time_s (Z-suffix so the
    # frontend's parseTimeStr accepts it).
    ev_dt = datetime.fromtimestamp(ref_epoch + float(result.origin_time_s), tz=timezone.utc)
    ev_time = ev_dt.isoformat().replace("+00:00", "Z")

    # Per-station ML using the grid-search hypocentre.
    ml_cfg = MLConfig(
        freq_hz=5.0,
        wa_gain=2800.0,
        min_distance_km=10.0,
        a=1.110,
        b=0.00189,
        c=3.0,
        ref_distance_km=100.0,
    )
    station_mls = []
    stations_by_id = locator.stations  # dict[str, Station]
    for meta in pick_meta:
        amp_vel = meta["amplitude"]
        if amp_vel is None:
            continue
        sta = stations_by_id.get(meta["station_id"])
        if sta is None:
            continue
        d_horiz = haversine_km(lat, lon, sta.latitude, sta.longitude)
        r = math.sqrt(d_horiz**2 + depth**2)
        ml_sta = compute_ml_station(float(amp_vel), r, ml_cfg)
        if ml_sta is not None:
            station_mls.append(ml_sta)

    ml, ml_err, ml_count = compute_ml_network(station_mls)

    # Residuals are rebased into the "seconds since event origin" frame
    # to match the old API contract and the frontend's residual table.
    t0 = float(result.origin_time_s)
    residuals = []
    for r in result.residuals:
        sta_short = r.station_id.split(".", 1)[-1]
        residuals.append(
            {
                "station": sta_short,
                "phase": r.phase,
                "observed_s": round(float(r.observed_s) - t0, 4),
                "predicted_s": round(float(r.predicted_s) - t0, 4),
                "residual_s": round(float(r.residual_s), 4),
            }
        )

    return {
        "location": {
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "depth_km": round(depth, 2),
            "time": ev_time,
            "sigma_time": round(float(result.rms_residual_s), 4),
            "sigma_amp": 0.0,  # grid-search locator doesn't model amplitude residuals
            "num_picks": int(result.n_picks_used),
            "sigma_depth_km": round(float(result.sigma_depth_km), 3),
            "ellipse_major_km": round(float(result.horizontal_semi_axis_major_km), 3),
            "ellipse_minor_km": round(float(result.horizontal_semi_axis_minor_km), 3),
            "ellipse_azimuth_deg": round(float(result.horizontal_semi_axis_azimuth_deg), 1),
        },
        "magnitude": {
            "ml": round(ml, 2) if ml is not None else None,
            "ml_err": round(ml_err, 2) if ml_err is not None else None,
            "num_ml_sta": ml_count,
        },
        "residuals": residuals,
    }


# ── Save reviewed event ──────────────────────────────────────────
@router.post("/api/event/{event_id}/save")
async def event_save(event_id: str, request: Request):
    """Persist a reviewed event: rewrite catalog row + replace its assignments."""
    body = await request.json()
    picks_input = body.get("picks", [])
    location = body.get("location", {})
    magnitude = body.get("magnitude", {})

    if not location:
        raise HTTPException(status_code=400, detail="Location data required")

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Whole read-modify-write must be serialized: concurrent reviewers
    # saving at the same time would both read the same starting state and
    # the second writer would overwrite the first writer's edits.
    async with _catalog_write_lock:
        # ── Update the matching catalog row in-place ──
        catalog_rows = read_catalog()
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

        atomic_write_csv(CATALOG_FILE, catalog_rows, _catalog_columns_from_rows(catalog_rows))

        # ── Replace assignments for this event with the new pick set ──
        others = [a for a in read_assignments() if a.get("event_id") != event_id]
        for p in picks_input:
            others.append(
                {
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
                }
            )
        atomic_write_csv(ASSIGNMENTS_FILE, others, _ASSIGNMENT_COLUMNS)

        clear_caches()

    duplicates = _near_duplicates(
        catalog_rows,
        event_id,
        location.get("time", ""),
        float(location["latitude"]),
        float(location["longitude"]),
    )
    result: dict = {"saved": True}
    if duplicates:
        result["duplicate_warning"] = (
            f"Possible duplicate(s) within {_DUP_DT_S:g}s / {_DUP_DIST_KM:g}km: "
            + ", ".join(duplicates)
            + " — consider scripts/dedup_catalog.py"
        )
    for row in catalog_rows:
        if row.get("event_id") == event_id:
            result["event"] = parse_catalog_row(row)
            break
    return result


# ── Quick status-only update (no relocation) ─────────────────────
@router.post("/api/event/{event_id}/review-status")
async def event_review_status(event_id: str, request: Request):
    """Confirm or reject an event without rerunning the locator."""
    body = await request.json()
    status = body.get("status", "")
    if status not in ("confirmed", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'confirmed' or 'rejected'")

    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Lock the read-modify-write — see comment on _catalog_write_lock above.
    async with _catalog_write_lock:
        catalog_rows = read_catalog()
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

        atomic_write_csv(CATALOG_FILE, catalog_rows, _catalog_columns_from_rows(catalog_rows))
        clear_caches()

    for row in catalog_rows:
        if row.get("event_id") == event_id:
            return {"event": parse_catalog_row(row), "updated": True}
    return {"updated": True}
