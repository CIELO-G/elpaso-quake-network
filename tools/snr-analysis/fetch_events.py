#!/usr/bin/env python3
"""
fetch_events.py — Query USGS ComCat for regional events and present candidates.

Also lists local catalog events for scenario selection.

Usage:
    python fetch_events.py                      # interactive selection
    python fetch_events.py --list-local         # just list local catalog events
    python fetch_events.py --list-regional      # just list ComCat candidates
    python fetch_events.py --auto               # auto-select best candidates and write config
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from obspy import UTCDateTime
from obspy.clients.fdsn import Client
from obspy.geodetics import gps2dist_azimuth

from utils import load_config


def list_local_events(cfg: dict) -> pd.DataFrame:
    """Load and display local catalog events."""
    catalog_file = cfg["catalog_file"]
    assignments_file = cfg["assignments_file"]

    cat = pd.read_csv(catalog_file)
    assignments = pd.read_csv(assignments_file)

    # Count unique stations per event
    sta_counts = (
        assignments.groupby("event_id")["station"]
        .nunique()
        .rename("num_stations")
    )
    cat = cat.merge(sta_counts, on="event_id", how="left")
    cat["num_stations"] = cat["num_stations"].fillna(0).astype(int)

    cat = cat.sort_values("num_picks", ascending=False)

    print("\n=== Local Catalog Events ===")
    print(f"{'event_id':<22} {'time':<26} {'mag':>5} {'picks':>6} {'stations':>9}")
    print("-" * 75)
    for _, r in cat.iterrows():
        print(f"{r['event_id']:<22} {r['time']:<26} {r['magnitude']:5.2f} "
              f"{r['num_picks']:6d} {r['num_stations']:9d}")

    return cat


def query_comcat(cfg: dict, min_mag: float, max_mag: float, label: str) -> pd.DataFrame:
    """Query USGS ComCat for events near El Paso."""
    search = cfg["catalog_search"]
    center_lat = cfg["center_lat"]
    center_lon = cfg["center_lon"]
    max_radius_km = search["max_radius_km"]
    # Convert km to degrees (approximate)
    max_radius_deg = max_radius_km / 111.19

    start = UTCDateTime(search["start_date"])
    end = UTCDateTime() if search.get("end_date") is None else UTCDateTime(search["end_date"])

    print(f"\n=== Querying USGS ComCat: {label} (M{min_mag}–{max_mag}) ===")
    print(f"  Center: {center_lat}, {center_lon}")
    print(f"  Radius: {max_radius_km} km ({max_radius_deg:.2f}°)")
    print(f"  Time: {start} → {end}")

    client = Client("USGS")
    try:
        cat = client.get_events(
            starttime=start,
            endtime=end,
            latitude=center_lat,
            longitude=center_lon,
            maxradius=max_radius_deg,
            minmagnitude=min_mag,
            maxmagnitude=max_mag,
            orderby="magnitude",
        )
    except Exception as exc:
        print(f"  Query failed: {exc}")
        return pd.DataFrame()

    rows = []
    for ev in cat:
        origin = ev.preferred_origin()
        mag = ev.preferred_magnitude()
        dist_m, _, _ = gps2dist_azimuth(
            center_lat, center_lon,
            origin.latitude, origin.longitude,
        )
        rows.append({
            "usgs_id": str(ev.resource_id).split("/")[-1],
            "time": str(origin.time),
            "latitude": origin.latitude,
            "longitude": origin.longitude,
            "depth_km": origin.depth / 1000.0 if origin.depth else 0.0,
            "magnitude": mag.mag if mag else 0.0,
            "mag_type": mag.magnitude_type if mag else "",
            "distance_km": dist_m / 1000.0,
            "description": str(ev.event_descriptions[0].text) if ev.event_descriptions else "",
        })

    df = pd.DataFrame(rows)
    if len(df) == 0:
        print("  No events found.")
        return df

    df = df.sort_values("magnitude", ascending=False)

    print(f"\n  Found {len(df)} event(s):")
    print(f"  {'#':>3} {'usgs_id':<20} {'time':<26} {'mag':>5} {'dist_km':>8} {'description'}")
    print("  " + "-" * 90)
    for i, (_, r) in enumerate(df.iterrows()):
        print(f"  {i:3d} {r['usgs_id']:<20} {r['time']:<26} {r['magnitude']:5.2f} "
              f"{r['distance_km']:8.1f} {r['description'][:40]}")

    return df


def select_event(df: pd.DataFrame, label: str) -> dict | None:
    """Prompt user to select an event from a dataframe."""
    if len(df) == 0:
        return None
    while True:
        choice = input(f"\n  Select {label} event # (or 'skip'): ").strip()
        if choice.lower() == "skip":
            return None
        try:
            idx = int(choice)
            row = df.iloc[idx]
            return row.to_dict()
        except (ValueError, IndexError):
            print(f"  Invalid choice. Enter 0–{len(df)-1} or 'skip'.")


def auto_select(local_df: pd.DataFrame, regional_low_df: pd.DataFrame,
                regional_high_df: pd.DataFrame) -> dict:
    """Auto-select the best candidates for each scenario."""
    selected = {}

    # Local: highest num_picks, break ties by magnitude
    if len(local_df) > 0:
        best = local_df.sort_values(["num_picks", "magnitude"],
                                     ascending=[False, False]).iloc[0]
        selected["local"] = {
            "event_id": best["event_id"],
            "origin_time": best["time"],
            "latitude": float(best["latitude"]),
            "longitude": float(best["longitude"]),
            "depth_km": float(best["depth_km"]),
            "magnitude": float(best["magnitude"]),
        }

    # Regional low: closest M2-ish event
    if len(regional_low_df) > 0:
        best = regional_low_df.sort_values("distance_km").iloc[0]
        selected["regional_low"] = {
            "event_id": best["usgs_id"],
            "origin_time": best["time"],
            "latitude": float(best["latitude"]),
            "longitude": float(best["longitude"]),
            "depth_km": float(best["depth_km"]),
            "magnitude": float(best["magnitude"]),
        }

    # Regional high: largest M3+ event
    if len(regional_high_df) > 0:
        best = regional_high_df.sort_values("magnitude", ascending=False).iloc[0]
        selected["regional_high"] = {
            "event_id": best["usgs_id"],
            "origin_time": best["time"],
            "latitude": float(best["latitude"]),
            "longitude": float(best["longitude"]),
            "depth_km": float(best["depth_km"]),
            "magnitude": float(best["magnitude"]),
        }

    return selected


def write_selected_events(selected: dict, output_dir: str) -> None:
    """Save selected events to JSON."""
    out = Path(output_dir) / "data" / "selected_events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(selected, f, indent=2, default=str)
    print(f"\nSaved selected events to {out}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and select events for SNR analysis")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--list-local", action="store_true", help="Only list local events")
    parser.add_argument("--list-regional", action="store_true", help="Only list ComCat events")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-select best candidates (no interactive prompts)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)

    if args.list_local:
        list_local_events(cfg)
        return

    # Query all event pools
    local_df = list_local_events(cfg)
    regional_low_df = query_comcat(cfg, min_mag=1.5, max_mag=2.5, label="Regional Low-Mag")
    regional_high_df = query_comcat(cfg, min_mag=3.0, max_mag=9.0, label="Regional High-Mag")

    if args.list_regional:
        return

    if args.auto:
        selected = auto_select(local_df, regional_low_df, regional_high_df)
        for scenario, ev in selected.items():
            print(f"\n  Auto-selected {scenario}: {ev['event_id']} "
                  f"M{ev['magnitude']:.2f} @ {ev['origin_time']}")
        write_selected_events(selected, cfg["output_dir"])
        return

    # Interactive selection
    selected = {}

    print("\n--- Select LOCAL event from catalog ---")
    while True:
        choice = input("  Enter event_id (e.g., ep20251122-0001) or 'skip': ").strip()
        if choice.lower() == "skip":
            break
        row = local_df[local_df["event_id"] == choice]
        if len(row) == 0:
            print(f"  Event '{choice}' not found in catalog.")
            continue
        r = row.iloc[0]
        selected["local"] = {
            "event_id": r["event_id"],
            "origin_time": r["time"],
            "latitude": float(r["latitude"]),
            "longitude": float(r["longitude"]),
            "depth_km": float(r["depth_km"]),
            "magnitude": float(r["magnitude"]),
        }
        break

    ev = select_event(regional_low_df, "regional low-mag")
    if ev:
        selected["regional_low"] = {
            "event_id": ev["usgs_id"],
            "origin_time": ev["time"],
            "latitude": float(ev["latitude"]),
            "longitude": float(ev["longitude"]),
            "depth_km": float(ev["depth_km"]),
            "magnitude": float(ev["magnitude"]),
        }

    ev = select_event(regional_high_df, "regional high-mag")
    if ev:
        selected["regional_high"] = {
            "event_id": ev["usgs_id"],
            "origin_time": ev["time"],
            "latitude": float(ev["latitude"]),
            "longitude": float(ev["longitude"]),
            "depth_km": float(ev["depth_km"]),
            "magnitude": float(ev["magnitude"]),
        }

    write_selected_events(selected, cfg["output_dir"])


if __name__ == "__main__":
    main()
