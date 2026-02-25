#!/usr/bin/env python3
"""
associate.py - Seismic event association and location

Step 4/5 of the El Paso seismic processing pipeline.
Groups detected phase arrivals into seismic events and estimates
hypocenter locations using GaMMA (Gaussian Mixture Model Association).

Usage (run from project root):
    python 4-association/associate.py --config 4-association/config.yaml
    python 4-association/associate.py --config 4-association/config.yaml --start 2026-01-29 --end 2026-01-29
    python 4-association/associate.py --config 4-association/config.yaml --force --debug
"""

from __future__ import annotations

import argparse
import csv
import gc
import math
import sys
import time
from pathlib import Path

# sklearn >=1.4 removed _check_n_features from estimator base classes;
# GaMMA's vendored mixture code still calls it.  Add a no-op shim so
# the association step works without pinning sklearn.
import sklearn.base
if not hasattr(sklearn.base.BaseEstimator, "_check_n_features"):
    def _check_n_features(self, X, reset=False):
        n_features = X.shape[1] if hasattr(X, "shape") and X.ndim > 1 else 1
        if reset or not hasattr(self, "n_features_in_"):
            self.n_features_in_ = n_features
    sklearn.base.BaseEstimator._check_n_features = _check_n_features

from obspy import UTCDateTime

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations
from lib.logger import MetricsWriter, setup_logging
from lib.magnitude import (
    MLConfig,
    compute_ml_network,
    compute_ml_station,
    haversine_km,
)

DEFAULTS = {
    "stations_file": "stations.json",
    "input_dir": "output",
    "output_dir": "output",
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
    "center_lat": 31.85,
    "center_lon": -106.40,
    "xlim_degree": 0.7,
    "ylim_degree": 0.7,
    "zlim_km": [0, 30],
    "degree2km": 111.19,
    "vel": {"P": 6.0, "S": 3.47},
    "method": "BGMM",
    "use_dbscan": True,
    "dbscan_eps": 25,
    "dbscan_min_samples": 3,
    "min_picks_per_eq": 4,
    "min_p_picks": 3,
    "min_s_picks": 1,
    "min_stations_per_eq": 3,
    "max_sigma11": 2.0,
    "max_sigma22": 2.0,
    "oversample_factor": 5,
    "min_pick_probability": 0.5,
    "assoc_window_hours": 24,
    "assoc_latency_hours": 6,
    # Local magnitude (ML) -- Hutton & Boore (1987) with Uhrhammer & Collins (1990) gain
    "ml_freq_hz": 5.0,
    "ml_wa_gain": 2800,
    "ml_min_distance": 10.0,
    "ml_a": 1.110,
    "ml_b": 0.00189,
    "ml_c": 3.0,
    "ml_ref_distance": 100.0,
}

EVENT_CSV_COLUMNS = [
    "event_index", "time", "magnitude", "magnitude_type", "ml_err", "latitude",
    "longitude", "depth_km", "sigma_time", "sigma_amp", "num_picks", "num_ml_sta",
]

ASSIGNMENT_CSV_COLUMNS = [
    "event_index", "network", "station", "location", "channel",
    "phase", "time", "probability", "amplitude", "amplitude_channel",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Associate phase picks into events and locate hypocenters",
    )
    parser.add_argument("--config", required=True,
                        help="Path to YAML configuration file")
    parser.add_argument("--start",
                        help="Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end",
                        help="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--force", action="store_true",
                        help="Re-associate even if output already exists")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Station DataFrame
# ---------------------------------------------------------------------------

def build_station_dataframe(stations: list[dict], config: dict, logger):
    """Convert stations.json list to GaMMA-format pandas DataFrame.

    Uses a stereographic projection centered on the network to convert
    lat/lon to x/y in km.  Returns ``(stations_df, proj)`` where *proj*
    is the pyproj Proj object for inverse-transforming results.
    """
    import pandas as pd
    from lib.projection import make_projection

    center_lon = config["center_lon"]
    center_lat = config["center_lat"]

    proj = make_projection(center_lon, center_lat)

    rows: list[dict] = []
    for s in stations:
        sid = f"{s['network']}.{s['station']}"
        x_km, y_km = proj(s["longitude"], s["latitude"])
        z_km = -s["elevation_m"] / 1000.0  # GaMMA: positive-down depth
        rows.append({
            "id": sid,
            "x(km)": x_km,
            "y(km)": y_km,
            "z(km)": z_km,
        })

    stations_df = pd.DataFrame(rows)
    logger.info("Built station DataFrame: %d stations", len(stations_df))
    logger.debug("Station coords (km):\n%s", stations_df.to_string())
    return stations_df, proj


# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

def get_daily_picks_path(input_dir: str, year: str, jday: str) -> Path:
    """Return path to Step 3 daily picks CSV."""
    return Path(input_dir) / "3-picks" / year / jday / f"{year}.{jday}.picks.csv"


def get_daily_output_paths(output_dir: str, year: str, jday: str) -> tuple[Path, Path]:
    """Return ``(catalog_path, assignments_path)`` for a day's association output."""
    base = Path(output_dir) / "4-events" / year / jday
    catalog = base / f"{year}.{jday}.events.csv"
    assignments = base / f"{year}.{jday}.assignments.csv"
    return catalog, assignments


# ---------------------------------------------------------------------------
# Pick loading
# ---------------------------------------------------------------------------

def load_daily_picks(picks_path: Path, config: dict, logger):
    """Read Step 3 picks CSV into GaMMA-format DataFrame."""
    import pandas as pd

    df = pd.read_csv(picks_path)
    if df.empty:
        logger.debug("No picks in %s", picks_path)
        return df

    # Build station ID
    df["id"] = df["network"] + "." + df["station"]

    # Map columns to GaMMA expected names
    df["timestamp"] = pd.to_datetime(df["time"])
    df["type"] = df["phase"]
    df["prob"] = df["probability"].astype(float)
    df["amp"] = df["amplitude"].astype(float)

    # Filter by minimum probability
    min_prob = config["min_pick_probability"]
    before = len(df)
    df = df[df["prob"] >= min_prob].copy()
    df.reset_index(drop=True, inplace=True)
    dropped = before - len(df)
    if dropped > 0:
        logger.debug("Filtered %d picks below probability %.2f", dropped, min_prob)

    logger.info("Loaded %d picks from %s", len(df), picks_path)
    return df


# ---------------------------------------------------------------------------
# GaMMA configuration
# ---------------------------------------------------------------------------

def build_gamma_config(config: dict, logger) -> dict:
    """Assemble the GaMMA config dict from pipeline config values."""
    import numpy as np

    center_lon = config["center_lon"]
    center_lat = config["center_lat"]
    xlim = config["xlim_degree"]
    ylim = config["ylim_degree"]
    degree2km = config["degree2km"]

    # Convert degree limits to km centred on (0,0) projection origin
    x_km = np.array([-xlim, xlim]) * degree2km * np.cos(np.deg2rad(center_lat))
    y_km = np.array([-ylim, ylim]) * degree2km
    z_km = config["zlim_km"]

    dims = ["x(km)", "y(km)", "z(km)"]

    gamma_config = {
        "center": (center_lon, center_lat),
        "xlim_degree": [-xlim, xlim],
        "ylim_degree": [-ylim, ylim],
        "degree2km": degree2km,
        "dims": dims,
        "x(km)": x_km.tolist(),
        "y(km)": y_km.tolist(),
        "z(km)": z_km,
        "use_amplitude": True,
        "vel": {k.lower(): v for k, v in config["vel"].items()},
        "use_dbscan": config["use_dbscan"],
        "dbscan_eps": config["dbscan_eps"],
        "dbscan_min_samples": config["dbscan_min_samples"],
        "min_picks_per_eq": config["min_picks_per_eq"],
        "max_sigma11": config["max_sigma11"],
        "max_sigma22": config["max_sigma22"],
        "oversample_factor": config["oversample_factor"],
    }

    # bfgs_bounds: spatial bounds + unconstrained time
    gamma_config["bfgs_bounds"] = (
        [gamma_config[d] for d in dims] + [[None, None]]
    )

    logger.debug("GaMMA config: %s", gamma_config)
    return gamma_config


# ---------------------------------------------------------------------------
# Association
# ---------------------------------------------------------------------------

def run_association(picks_df, stations_df, gamma_config: dict, config: dict, logger):
    """Run GaMMA association on a day's picks."""
    try:
        from gamma.utils import association
    except ImportError as exc:
        logger.critical("Cannot import gamma: %s", exc)
        sys.exit(1)

    method = config["method"]
    logger.info("Running GaMMA association (method=%s, %d picks)", method, len(picks_df))

    try:
        events_list, assignments_list = association(
            picks_df, stations_df, gamma_config, method=method,
        )
        return events_list, assignments_list
    except Exception as exc:
        logger.error("GaMMA association failed: %s: %s", type(exc).__name__, exc)
        return None, None


# ---------------------------------------------------------------------------
# Post-association QC
# ---------------------------------------------------------------------------

def filter_events_by_station_count(
    events_list: list,
    assignments_list: list,
    picks_df,
    config: dict,
    logger,
) -> tuple[list, list]:
    """Drop events whose picks come from fewer than ``min_stations_per_eq`` unique stations."""
    min_stations = config["min_stations_per_eq"]

    # Count unique stations per event
    event_stations: dict[int, set[str]] = {}
    for pick_idx, event_idx, _score in assignments_list:
        pick = picks_df.iloc[pick_idx]
        sta_id = f"{pick.get('network', '')}.{pick.get('station', '')}"
        event_stations.setdefault(event_idx, set()).add(sta_id)

    keep: set[int] = set()
    for event_idx, stations in event_stations.items():
        if len(stations) >= min_stations:
            keep.add(event_idx)
        else:
            logger.info("Dropping event %d: %d station(s) (%s), need >= %d",
                        event_idx, len(stations), ", ".join(sorted(stations)), min_stations)

    filtered_events = [ev for ev in events_list if ev.get("event_index", 0) in keep]
    filtered_assignments = [(pi, ei, sc) for pi, ei, sc in assignments_list if ei in keep]

    dropped = len(events_list) - len(filtered_events)
    if dropped:
        logger.info("Station-count filter: kept %d, dropped %d event(s)",
                    len(filtered_events), dropped)
    return filtered_events, filtered_assignments


def filter_events_by_phase_count(
    events_list: list,
    assignments_list: list,
    picks_df,
    config: dict,
    logger,
) -> tuple[list, list]:
    """Drop events that don't meet minimum P-pick and S-pick requirements."""
    min_p = config.get("min_p_picks", 0)
    min_s = config.get("min_s_picks", 0)
    if min_p == 0 and min_s == 0:
        return events_list, assignments_list

    # Count P and S picks per event
    event_phases: dict[int, dict[str, int]] = {}
    for pick_idx, event_idx, _score in assignments_list:
        pick = picks_df.iloc[pick_idx]
        phase = pick.get("type", pick.get("phase", ""))
        counts = event_phases.setdefault(event_idx, {"P": 0, "S": 0})
        if phase in ("P", "S"):
            counts[phase] += 1

    keep: set[int] = set()
    for event_idx, counts in event_phases.items():
        if counts["P"] >= min_p and counts["S"] >= min_s:
            keep.add(event_idx)
        else:
            logger.info("Dropping event %d: %dP + %dS picks, need >= %dP + %dS",
                        event_idx, counts["P"], counts["S"], min_p, min_s)

    filtered_events = [ev for ev in events_list if ev.get("event_index", 0) in keep]
    filtered_assignments = [(pi, ei, sc) for pi, ei, sc in assignments_list if ei in keep]

    dropped = len(events_list) - len(filtered_events)
    if dropped:
        logger.info("Phase-count filter: kept %d, dropped %d event(s)",
                    len(filtered_events), dropped)
    return filtered_events, filtered_assignments


# ---------------------------------------------------------------------------
# Station lookup & local magnitude (ML)
# ---------------------------------------------------------------------------

def build_station_lookup(stations: list[dict]) -> dict[str, dict]:
    """Return dict mapping ``"NET.STA"`` to station lat/lon/elevation."""
    lookup: dict[str, dict] = {}
    for s in stations:
        key = f"{s['network']}.{s['station']}"
        lookup[key] = {
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "elevation_m": s["elevation_m"],
        }
    return lookup


def compute_event_ml(
    event_lat: float,
    event_lon: float,
    event_depth_km: float,
    event_idx: int,
    assignments_list: list,
    picks_df,
    station_lookup: dict[str, dict],
    config: dict,
    logger,
) -> tuple[float | None, float | None, int]:
    """Compute local magnitude (ML) for one event using the lib/magnitude module.

    Uses Hutton & Boore (1987) with Uhrhammer & Collins (1990) WA gain.
    ML uncertainty is standard deviation of station readings (Issue 18).
    """
    ml_cfg = MLConfig(
        freq_hz=config["ml_freq_hz"],
        wa_gain=config["ml_wa_gain"],
        min_distance_km=config["ml_min_distance"],
        a=config["ml_a"],
        b=config["ml_b"],
        c=config["ml_c"],
        ref_distance_km=config["ml_ref_distance"],
    )

    station_mls: list[float] = []

    for pick_idx, ev_idx, _score in assignments_list:
        if ev_idx != event_idx:
            continue

        pick = picks_df.iloc[pick_idx]
        amp_vel = pick.get("amplitude", pick.get("amp", None))
        if amp_vel is None or amp_vel <= 0:
            continue

        sta_id = f"{pick.get('network', '')}.{pick.get('station', '')}"
        sta = station_lookup.get(sta_id)
        if sta is None:
            continue

        d_horiz = haversine_km(event_lat, event_lon, sta["latitude"], sta["longitude"])
        r = math.sqrt(d_horiz ** 2 + event_depth_km ** 2)

        ml_sta = compute_ml_station(float(amp_vel), r, ml_cfg)
        if ml_sta is not None:
            station_mls.append(ml_sta)
            logger.debug("ML %s: amp_vel=%.3e  r=%.1f km  ML_sta=%.2f",
                         sta_id, amp_vel, r, ml_sta)

    return compute_ml_network(station_mls)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_catalog(
    events_list: list,
    proj,
    gamma_config: dict,
    assignments_list: list,
    picks_df,
    station_lookup: dict[str, dict],
    config: dict,
    logger,
) -> list[dict]:
    """Convert GaMMA event dicts to final CSV rows."""
    rows: list[dict] = []
    for ev in events_list:
        x = ev.get("x(km)", 0.0)
        y = ev.get("y(km)", 0.0)
        depth = ev.get("z(km)", 0.0)
        event_idx = ev.get("event_index", 0)

        lon, lat = proj(x, y, inverse=True)

        num_picks = ev.get("num_picks", ev.get("num_p", 0) + ev.get("num_s", 0))

        # Compute local magnitude from pick amplitudes
        ml, ml_err, ml_count = compute_event_ml(
            lat, lon, depth, event_idx,
            assignments_list, picks_df, station_lookup, config, logger,
        )

        rows.append({
            "event_index": int(event_idx),
            "time": str(ev.get("time", "")),
            "magnitude": f"{ml:.2f}" if ml is not None else "",
            "magnitude_type": "ML" if ml is not None else "",
            "ml_err": f"{ml_err:.2f}" if ml_err is not None else "",
            "latitude": f"{lat:.6f}",
            "longitude": f"{lon:.6f}",
            "depth_km": f"{depth:.2f}",
            "sigma_time": f"{ev.get('sigma_time', 0.0):.4f}",
            "sigma_amp": f"{ev.get('sigma_amp', 0.0):.4f}",
            "num_picks": int(num_picks),
            "num_ml_sta": int(ml_count),
        })

    logger.info("Formatted %d events", len(rows))
    return rows


def format_assignments(assignments_list: list, picks_df, logger) -> list[dict]:
    """Convert GaMMA assignment tuples to final CSV rows."""
    rows: list[dict] = []
    for pick_idx, event_idx, _score in assignments_list:
        pick = picks_df.iloc[pick_idx]

        rows.append({
            "event_index": int(event_idx),
            "network": pick.get("network", ""),
            "station": pick.get("station", ""),
            "location": pick.get("location", ""),
            "channel": pick.get("channel", ""),
            "phase": pick.get("phase", pick.get("type", "")),
            "time": str(pick.get("time", pick.get("timestamp", ""))),
            "probability": pick.get("probability", pick.get("prob", "")),
            "amplitude": pick.get("amplitude", pick.get("amp", "")),
            "amplitude_channel": pick.get("amplitude_channel", ""),
        })

    logger.info("Formatted %d pick assignments", len(rows))
    return rows


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

def write_catalog_csv(rows: list[dict], output_path: Path, logger) -> None:
    """Write event catalog CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info("Wrote %d event(s) to %s", len(rows), output_path)


def write_assignments_csv(rows: list[dict], output_path: Path, logger) -> None:
    """Write pick-assignment CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ASSIGNMENT_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info("Wrote %d assignment(s) to %s", len(rows), output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("associate", config, debug=args.debug)
    metrics = MetricsWriter(Path(config["log_dir"]) / "metrics.jsonl")

    logger.info("=" * 60)
    logger.info("Event association & location -- %d station(s)", len(stations))
    logger.info("=" * 60)

    # Determine time window
    if args.start and args.end:
        start_time = UTCDateTime(args.start)
        end_time = UTCDateTime(args.end)
        logger.info("Explicit range: %s -> %s", start_time, end_time)
    elif args.start or args.end:
        logger.error("Both --start and --end are required together")
        sys.exit(1)
    else:
        end_time = UTCDateTime() - config["assoc_latency_hours"] * 3600
        start_time = end_time - config["assoc_window_hours"] * 3600
        logger.info("Scheduled mode: %s -> %s", start_time, end_time)

    force = args.force
    if force:
        logger.info("Force mode enabled -- re-associating all days")

    # Build station DataFrame and projection (once)
    stations_df, proj = build_station_dataframe(stations, config, logger)

    # Build station lookup for ML computation (once)
    station_lookup = build_station_lookup(stations)

    # Build GaMMA config (once)
    gamma_config = build_gamma_config(config, logger)

    # Iterate days
    totals: dict[str, int] = {"days": 0, "skipped": 0, "events": 0, "picks_associated": 0, "failed": 0}

    day = UTCDateTime(start_time.year, start_time.month, start_time.day)
    # Subtract 1 second so midnight end times stay on the previous day
    end_adj = end_time - 1
    end_day = UTCDateTime(end_adj.year, end_adj.month, end_adj.day)

    while day <= end_day:
        year = str(day.year)
        jday = f"{day.julday:03d}"

        catalog_path, assignments_path = get_daily_output_paths(config["output_dir"], year, jday)

        # Skip check
        if catalog_path.exists() and not force:
            logger.debug("Already associated, skipping day %s/%s", year, jday)
            totals["skipped"] += 1
            day += 86400
            continue

        logger.info("=== Day %s/%s ===", year, jday)
        day_t0 = time.monotonic()

        # Load picks
        picks_path = get_daily_picks_path(config["input_dir"], year, jday)
        if not picks_path.exists():
            logger.warning("No picks file for %s/%s: %s", year, jday, picks_path)
            # Write empty outputs so skip logic works next time
            write_catalog_csv([], catalog_path, logger)
            write_assignments_csv([], assignments_path, logger)
            totals["days"] += 1
            day += 86400
            continue

        try:
            picks_df = load_daily_picks(picks_path, config, logger)
        except (ValueError, KeyError) as exc:
            logger.error("Failed to load picks for %s/%s: %s: %s",
                         year, jday, type(exc).__name__, exc)
            totals["failed"] += 1
            day += 86400
            continue

        if picks_df.empty:
            logger.info("No picks after filtering for %s/%s", year, jday)
            write_catalog_csv([], catalog_path, logger)
            write_assignments_csv([], assignments_path, logger)
            totals["days"] += 1
            day += 86400
            continue

        # Run association
        events_list, assignments_list = run_association(
            picks_df, stations_df, gamma_config, config, logger,
        )

        if events_list is None or assignments_list is None:
            logger.error("Association returned no results for %s/%s", year, jday)
            write_catalog_csv([], catalog_path, logger)
            write_assignments_csv([], assignments_path, logger)
            totals["failed"] += 1
            totals["days"] += 1
            day += 86400
            continue

        # Drop events that don't span enough stations
        events_list, assignments_list = filter_events_by_station_count(
            events_list, assignments_list, picks_df, config, logger,
        )

        # Drop events that don't meet P/S phase requirements
        events_list, assignments_list = filter_events_by_phase_count(
            events_list, assignments_list, picks_df, config, logger,
        )

        n_events = len(events_list)
        n_assigned = len(assignments_list)

        if n_events == 0:
            logger.info("No events found for %s/%s", year, jday)
            write_catalog_csv([], catalog_path, logger)
            write_assignments_csv([], assignments_path, logger)
            totals["days"] += 1
            day += 86400
            continue

        # Format and write outputs
        try:
            event_rows = format_catalog(
                        events_list, proj, gamma_config, assignments_list,
                        picks_df, station_lookup, config, logger)
            assignment_rows = format_assignments(assignments_list, picks_df, logger)
            write_catalog_csv(event_rows, catalog_path, logger)
            write_assignments_csv(assignment_rows, assignments_path, logger)
            totals["events"] += n_events
            totals["picks_associated"] += n_assigned
        except Exception as exc:
            logger.error("Failed to write results for %s/%s: %s: %s",
                         year, jday, type(exc).__name__, exc)
            totals["failed"] += 1

        day_elapsed = time.monotonic() - day_t0
        metrics.record(
            "associate",
            day=f"{year}/{jday}",
            events=n_events,
            picks_associated=n_assigned,
            duration_s=round(day_elapsed, 1),
        )

        totals["days"] += 1

        # Explicit cleanup after each day
        gc.collect()

        day += 86400

    # Summary
    logger.info("=" * 60)
    logger.info(
        "Done. days=%d  skipped=%d  events=%d  picks_associated=%d  failed=%d",
        totals["days"], totals["skipped"], totals["events"],
        totals["picks_associated"], totals["failed"],
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
