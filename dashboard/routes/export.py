"""Catalog export endpoints: CSV (flat) and QuakeML (ObsPy)."""

from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from starlette.responses import Response

from dashboard.deps import filter_by_date, read_assignments, read_catalog

router = APIRouter()


# ── Flat CSV ─────────────────────────────────────────────────────
@router.get("/api/catalog/export")
async def catalog_export(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Filtered catalog as a flat CSV download."""
    rows = read_catalog()
    if start_date or end_date:
        rows = filter_by_date(rows, start_date, end_date)

    if not rows:
        raise HTTPException(
            status_code=404, detail="No catalog data available for the selected date range"
        )

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


# ── QuakeML (via ObsPy) ──────────────────────────────────────────
@router.get("/api/catalog/export/quakeml")
async def catalog_export_quakeml(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Filtered catalog as QuakeML (origins + magnitudes + picks)."""
    from obspy import UTCDateTime
    from obspy.core.event import (
        Catalog as ObsCatalog,
        Event as ObsEvent,
        Magnitude,
        Origin,
        Pick as ObsPick,
        WaveformStreamID,
    )

    rows = read_catalog()
    if start_date or end_date:
        rows = filter_by_date(rows, start_date, end_date)

    if not rows:
        raise HTTPException(
            status_code=404, detail="No catalog data for the selected date range"
        )

    # Index assignments by event_id for O(1) lookup while building the catalog.
    assign_by_event: dict[str, list[dict]] = {}
    for a in read_assignments():
        eid = a.get("event_id", "")
        if eid:
            assign_by_event.setdefault(eid, []).append(a)

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
            ev.picks.append(ObsPick(
                time=UTCDateTime(pick_time),
                phase_hint=a.get("phase", ""),
                waveform_id=wf_id,
            ))

        obs_catalog.append(ev)

    buf = io.BytesIO()
    obs_catalog.write(buf, format="QUAKEML")
    xml_bytes = buf.getvalue()

    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=catalog_export.xml"},
    )
