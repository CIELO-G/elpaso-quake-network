#!/usr/bin/env python3
"""
catalog.py - Running seismic event catalog

Step 5/5 of the El Paso seismic processing pipeline.
Scans daily event files from step 4, merges them into a single running
catalog CSV with globally unique event IDs, and does the same for
pick assignments. The catalog is updated incrementally — re-running
appends new days without duplicating existing ones.

Usage (run from project root):
    python 5-catalog/catalog.py --config 5-catalog/config.yaml
    python 5-catalog/catalog.py --config 5-catalog/config.yaml --debug
    python 5-catalog/catalog.py --config 5-catalog/config.yaml --rebuild
"""

import argparse
import datetime
import sys
from pathlib import Path

import pandas as pd

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations
from lib.logger import setup_logging

DEFAULTS = {
    "stations_file": "stations.json",
    "input_dir": "output",
    "output_dir": "output",
    "catalog_file": "5-catalog/catalog.csv",
    "assignments_file": "5-catalog/assignments.csv",
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate running seismic event catalog",
    )
    parser.add_argument("--config", required=True,
                        help="Path to YAML configuration file")
    parser.add_argument("--rebuild", action="store_true",
                        help="Force full catalog rebuild from scratch")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging")
    return parser.parse_args()


def _jday_to_date(year, jday):
    """Convert year + julian day to a YYYYMMDD string."""
    dt = datetime.date(int(year), 1, 1) + datetime.timedelta(days=int(jday) - 1)
    return dt.strftime("%Y%m%d")


def make_event_id(year, jday, event_index):
    """Build a globally unique event ID: ep{YYYYMMDD}-{NNNN}."""
    return f"ep{_jday_to_date(year, jday)}-{int(event_index):04d}"


def day_key(year, jday):
    """Return the YYYYMMDD string used to track which days are cataloged."""
    return _jday_to_date(year, jday)


def discover_daily_files(input_dir, logger):
    """Find all daily events/assignments CSV pairs under 4-events/."""
    events_dir = Path(input_dir) / "4-events"
    pattern = "**/*.events.csv"
    event_files = sorted(events_dir.glob(pattern))
    logger.debug("Found %d daily event file(s) under %s", len(event_files), events_dir)

    daily = []
    for ef in event_files:
        # Expected: {input_dir}/4-events/{year}/{jday}/{year}.{jday}.events.csv
        stem = ef.stem.replace(".events", "")  # "2026.001"
        parts = stem.split(".")
        if len(parts) != 2:
            logger.warning("Skipping unexpected filename: %s", ef.name)
            continue
        year, jday = parts[0], parts[1]
        af = ef.parent / f"{year}.{jday}.assignments.csv"
        daily.append({
            "year": year,
            "jday": jday,
            "day_key": day_key(year, jday),
            "events_path": ef,
            "assignments_path": af,
        })

    return daily


def load_existing_catalog(catalog_path, logger):
    """Load existing catalog if present; return (DataFrame, set-of-day-keys)."""
    if not catalog_path.exists():
        return pd.DataFrame(), set()

    df = pd.read_csv(catalog_path, dtype={"event_id": str})
    # Extract day keys from event_id: ep{YYYYMMDD}-NNNN → YYYYMMDD
    existing_days = set(df["event_id"].str[2:10])
    logger.info("Existing catalog: %d events covering %d day(s)",
                len(df), len(existing_days))
    return df, existing_days


def load_existing_assignments(assignments_path, logger):
    """Load existing assignments if present."""
    if not assignments_path.exists():
        return pd.DataFrame()

    df = pd.read_csv(assignments_path, dtype={"event_id": str})
    logger.debug("Existing assignments: %d rows", len(df))
    return df


def main():
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("catalog", config, debug=args.debug)

    logger.info("=" * 60)
    logger.info("Running catalog — %d station(s)", len(stations))
    logger.info("=" * 60)

    input_dir = Path(config["input_dir"])
    output_dir = Path(config["output_dir"])
    catalog_path = output_dir / config["catalog_file"]
    assignments_path = output_dir / config["assignments_file"]

    # Ensure output directory exists
    catalog_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Discover daily files ─────────────────────────────────────────
    daily_files = discover_daily_files(input_dir, logger)
    if not daily_files:
        logger.warning("No daily event files found — nothing to catalog")
        return

    logger.info("Discovered %d day(s) with event files", len(daily_files))

    # ── Load existing catalog (unless rebuilding) ────────────────────
    if args.rebuild:
        logger.info("Rebuild mode — ignoring existing catalog")
        existing_catalog = pd.DataFrame()
        existing_assignments = pd.DataFrame()
        existing_days = set()
    else:
        existing_catalog, existing_days = load_existing_catalog(catalog_path, logger)
        existing_assignments = load_existing_assignments(assignments_path, logger)

    # ── Process new days ─────────────────────────────────────────────
    new_event_dfs = []
    new_assign_dfs = []
    new_day_count = 0

    for day in daily_files:
        dk = day["day_key"]
        if dk in existing_days:
            logger.debug("Day %s already in catalog, skipping", dk)
            continue

        # Read events
        events_path = day["events_path"]
        try:
            events = pd.read_csv(events_path)
        except Exception as e:
            logger.warning("Failed to read %s: %s", events_path, e)
            continue

        if events.empty:
            logger.debug("Day %s has no events, skipping", dk)
            continue

        # Build global event IDs
        events["event_id"] = events["event_index"].apply(
            lambda idx: make_event_id(day["year"], day["jday"], idx)
        )

        # Reorder columns: event_id first
        cols = ["event_id"] + [c for c in events.columns if c != "event_id"]
        events = events[cols]

        new_event_dfs.append(events)
        logger.debug("Day %s: %d event(s)", dk, len(events))

        # Read assignments (if file exists)
        assign_path = day["assignments_path"]
        if assign_path.exists():
            try:
                assigns = pd.read_csv(assign_path)
            except Exception as e:
                logger.warning("Failed to read %s: %s", assign_path, e)
                assigns = pd.DataFrame()

            if not assigns.empty:
                # Map event_index -> event_id
                idx_to_id = dict(zip(events["event_index"], events["event_id"]))
                assigns["event_id"] = assigns["event_index"].map(idx_to_id)
                # Drop event_index, put event_id first
                assigns = assigns.drop(columns=["event_index"])
                acols = ["event_id"] + [c for c in assigns.columns if c != "event_id"]
                assigns = assigns[acols]
                new_assign_dfs.append(assigns)

        new_day_count += 1

    # ── Merge ────────────────────────────────────────────────────────
    if not new_event_dfs:
        logger.info("No new days to add — catalog is up to date")
        return

    new_events = pd.concat(new_event_dfs, ignore_index=True)
    new_assigns = pd.concat(new_assign_dfs, ignore_index=True) if new_assign_dfs else pd.DataFrame()

    if not existing_catalog.empty:
        catalog = pd.concat([existing_catalog, new_events], ignore_index=True)
    else:
        catalog = new_events

    if not existing_assignments.empty and not new_assigns.empty:
        assignments = pd.concat([existing_assignments, new_assigns], ignore_index=True)
    elif not new_assigns.empty:
        assignments = new_assigns
    else:
        assignments = existing_assignments

    # Sort chronologically
    catalog = catalog.sort_values("time").reset_index(drop=True)
    if not assignments.empty:
        assignments = assignments.sort_values("time").reset_index(drop=True)

    # ── Write ────────────────────────────────────────────────────────
    catalog.to_csv(catalog_path, index=False)
    logger.info("Wrote catalog: %s (%d events)", catalog_path, len(catalog))

    if not assignments.empty:
        assignments.to_csv(assignments_path, index=False)
        logger.info("Wrote assignments: %s (%d rows)", assignments_path, len(assignments))

    # ── Summary ──────────────────────────────────────────────────────
    time_min = catalog["time"].min()
    time_max = catalog["time"].max()
    logger.info("-" * 60)
    logger.info("Catalog summary:")
    logger.info("  Total events   : %d", len(catalog))
    logger.info("  New events     : %d (from %d day(s))", len(new_events), new_day_count)
    logger.info("  Date range     : %s — %s", time_min, time_max)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
