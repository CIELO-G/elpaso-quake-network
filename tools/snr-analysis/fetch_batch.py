#!/usr/bin/env python3
"""
fetch_batch.py — Fetch 100 regional earthquakes from USGS ComCat for batch SNR analysis.

Queries M2+ events in West Texas / Permian Basin within the network's operational
period, saves them to output/data/batch_events.json.

Usage:
    python fetch_batch.py                    # fetch 100 events
    python fetch_batch.py --count 50         # fetch 50 events
    python fetch_batch.py --min-mag 2.5      # M2.5+ only
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from obspy import UTCDateTime
from obspy.clients.fdsn import Client
from obspy.geodetics import gps2dist_azimuth

from utils import load_config


def fetch_regional_events(cfg: dict, count: int, min_mag: float, max_mag: float,
                          max_dist_km: float = 300.0) -> list[dict]:
    """Query USGS ComCat for regional events."""
    search = cfg["catalog_search"]
    center_lat = cfg["center_lat"]
    center_lon = cfg["center_lon"]
    max_radius_km = search["max_radius_km"]
    max_radius_deg = max_radius_km / 111.19

    start = UTCDateTime(search["start_date"])
    end = UTCDateTime() if search.get("end_date") is None else UTCDateTime(search["end_date"])

    print(f"Querying USGS ComCat: M{min_mag}–{max_mag}")
    print(f"  Center: {center_lat}, {center_lon}")
    print(f"  Radius: {max_radius_km} km")
    print(f"  Time: {start} → {end}")
    print(f"  Target: {count} events")

    client = Client("USGS")
    cat = client.get_events(
        starttime=start,
        endtime=end,
        latitude=center_lat,
        longitude=center_lon,
        maxradius=max_radius_deg,
        minmagnitude=min_mag,
        maxmagnitude=max_mag,
        orderby="time",
        limit=count * 2,  # fetch extra in case some are duplicates
    )

    events = []
    seen_times = set()

    for ev in cat:
        origin = ev.preferred_origin()
        mag = ev.preferred_magnitude()
        if origin is None or mag is None:
            continue

        # Deduplicate by rounding origin time to nearest second
        time_key = str(origin.time)[:19]
        if time_key in seen_times:
            continue
        seen_times.add(time_key)

        dist_m, _, _ = gps2dist_azimuth(
            center_lat, center_lon,
            origin.latitude, origin.longitude,
        )

        # Extract a clean event ID from the resource_id
        rid = str(ev.resource_id)
        event_id = rid.split("=")[-1] if "=" in rid else rid.split("/")[-1]

        dist_km = dist_m / 1000.0
        if dist_km > max_dist_km:
            continue

        events.append({
            "event_id": event_id,
            "origin_time": str(origin.time),
            "latitude": origin.latitude,
            "longitude": origin.longitude,
            "depth_km": origin.depth / 1000.0 if origin.depth else 0.0,
            "magnitude": mag.mag,
            "mag_type": mag.magnitude_type,
            "distance_km": round(dist_km, 1),
            "description": str(ev.event_descriptions[0].text) if ev.event_descriptions else "",
        })

        if len(events) >= count:
            break

    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch regional events for batch SNR analysis")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--count", type=int, default=100, help="Number of events to fetch")
    parser.add_argument("--min-mag", type=float, default=2.5, help="Minimum magnitude")
    parser.add_argument("--max-mag", type=float, default=9.0, help="Maximum magnitude")
    parser.add_argument("--max-dist", type=float, default=300.0, help="Max distance in km")
    args = parser.parse_args()

    cfg = load_config(args.config)
    events = fetch_regional_events(cfg, args.count, args.min_mag, args.max_mag, args.max_dist)

    print(f"\nFetched {len(events)} events")

    # Summary
    mags = [e["magnitude"] for e in events]
    dists = [e["distance_km"] for e in events]
    print(f"  Magnitude range: {min(mags):.1f} – {max(mags):.1f}")
    print(f"  Distance range:  {min(dists):.0f} – {max(dists):.0f} km")

    # Breakdown
    m2 = sum(1 for m in mags if m < 3.0)
    m3 = sum(1 for m in mags if 3.0 <= m < 4.0)
    m4 = sum(1 for m in mags if m >= 4.0)
    print(f"  M2–3: {m2} | M3–4: {m3} | M4+: {m4}")

    # Save
    out = Path(cfg["output_dir"]) / "data" / "batch_events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(events, f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
