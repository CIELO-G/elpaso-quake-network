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
import math
import sys
from pathlib import Path

import pandas as pd

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.atomic_io import atomic_write_df
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
    parser.add_argument("--config", required=True, help="Path to YAML configuration file")
    parser.add_argument(
        "--rebuild", action="store_true", help="Force full catalog rebuild from scratch"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    return parser.parse_args()


def _jday_to_date(year, jday):
    """Convert year + julian day to a YYYYMMDD string."""
    dt = datetime.date(int(year), 1, 1) + datetime.timedelta(days=int(jday) - 1)
    return dt.strftime("%Y%m%d")


def make_event_id(year, jday, event_index):
    """Build a globally unique event ID: ep{YYYYMMDD}-{NNNN}."""
    return f"ep{_jday_to_date(year, jday)}-{int(event_index):04d}"


# ── Duplicate-identity thresholds ─────────────────────────────────────
# Two catalog rows within BOTH thresholds are treated as the same physical
# event. Keep in sync with scripts/dedup_catalog.py and the dashboard's
# save-time guard (dashboard/routes/review.py).
DUP_DT_S = 2.0
DUP_DIST_KM = 5.0
_DEG2KM = 111.19


def _parse_utc(iso):
    """ISO string → aware UTC datetime (tolerates 'Z'-suffixed and naive)."""
    t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t


def _same_event(time_a, lat_a, lon_a, time_b, lat_b, lon_b):
    """Content-identity test: origins within DUP_DT_S and DUP_DIST_KM."""
    try:
        dt = abs((_parse_utc(time_a) - _parse_utc(time_b)).total_seconds())
        lat_a, lon_a, lat_b, lon_b = (float(lat_a), float(lon_a),
                                      float(lat_b), float(lon_b))
    except (ValueError, TypeError):
        return False
    if dt > DUP_DT_S:
        return False
    dlat = (lat_b - lat_a) * _DEG2KM
    dlon = (lon_b - lon_a) * _DEG2KM * math.cos(math.radians((lat_a + lat_b) / 2))
    return math.hypot(dlat, dlon) <= DUP_DIST_KM


def restore_reviewed_events(catalog, assignments, preserved_events,
                            preserved_assignments, logger):
    """Overlay snapshot-preserved reviewed events onto a rebuilt catalog.

    Event IDs are positional (GaMMA's per-day event_index), so
    re-associating a day can renumber its events. Matching the snapshot
    back purely by event_id has two failure modes this function guards
    against:

    * the reviewed event came back under a NEW id — the id-keyed orphan
      restore used to append the old row NEXT TO its fresh twin, producing
      duplicate catalog rows (bug fixed 2026-07-05; 7 pairs cleaned);
    * the old id was REUSED by a different event — a blind in-place
      overwrite would silently replace that new event with stale data.

    Identity is therefore verified by content (±DUP_DT_S s / DUP_DIST_KM
    km) before either restore path, and an unreviewed fresh twin of a
    reclaimed reviewed row is dropped rather than duplicated.
    """
    if not preserved_events:
        return catalog, assignments

    for col in ("reviewed", "review_status", "event_type"):
        if col not in catalog.columns:
            catalog[col] = ""

    restored_present = 0
    reclaimed = 0
    restored_orphan = 0
    twin_ids: set = set()
    append_rows = []

    for eid, row_data in preserved_events.items():
        mask = catalog["event_id"].astype(str) == eid
        if mask.any():
            current = catalog[mask].iloc[0]
            if _same_event(row_data.get("time"), row_data.get("latitude"),
                           row_data.get("longitude"), current.get("time"),
                           current.get("latitude"), current.get("longitude")):
                for col, val in row_data.items():
                    if col in catalog.columns:
                        catalog.loc[mask, col] = val
                restored_present += 1
                continue
            logger.warning(
                "Event id %s now belongs to a different event after "
                "re-association (preserved t=%s vs new t=%s) — keeping the "
                "new event, restoring the reviewed row alongside it",
                eid, row_data.get("time"), current.get("time"),
            )
        # Orphan path: the reviewed row has no (content-matching) id in the
        # new generation. If its fresh twin exists under a new id, drop the
        # twin — the reviewed row IS that event.
        twin = None
        for _, cand in catalog.iterrows():  # O(n·m); fine at catalog scale
            cid = str(cand.get("event_id"))
            if cid == eid or cid in twin_ids:
                continue
            if str(cand.get("review_status") or "").strip():
                continue  # never silently drop another reviewed row
            if _same_event(row_data.get("time"), row_data.get("latitude"),
                           row_data.get("longitude"), cand.get("time"),
                           cand.get("latitude"), cand.get("longitude")):
                twin = cid
                break
        if twin is not None:
            twin_ids.add(twin)
            reclaimed += 1
            logger.info("Reviewed event %s reclaimed its re-associated twin %s",
                        eid, twin)
        else:
            restored_orphan += 1
        append_rows.append({col: row_data.get(col, "") for col in catalog.columns})

    if twin_ids:
        catalog = catalog[~catalog["event_id"].astype(str).isin(twin_ids)]
        catalog = catalog.reset_index(drop=True)
        if not assignments.empty:
            assignments = assignments[
                ~assignments["event_id"].astype(str).isin(twin_ids)
            ].reset_index(drop=True)
    if append_rows:
        catalog = pd.concat([catalog, pd.DataFrame(append_rows)], ignore_index=True)

    # Replace assignments for preserved events with the snapshot's picks.
    if preserved_assignments:
        if not assignments.empty:
            keep_mask = ~assignments["event_id"].astype(str).isin(preserved_assignments.keys())
            assignments = assignments[keep_mask].reset_index(drop=True)
        rebuilt_rows = [r for rows in preserved_assignments.values() for r in rows]
        if rebuilt_rows:
            preserved_df = pd.DataFrame(rebuilt_rows)
            assignments = (
                pd.concat([assignments, preserved_df], ignore_index=True)
                if not assignments.empty
                else preserved_df
            )

    logger.info(
        "Restored %d reviewed row(s) in-place, %d reclaimed a re-associated "
        "twin, %d orphan(s)",
        restored_present, reclaimed, restored_orphan,
    )
    return catalog, assignments


def warn_near_duplicates(catalog, logger):
    """Post-merge safety net: warn on near-duplicate pairs in the catalog."""
    try:
        rows = catalog[["event_id", "time", "latitude", "longitude"]].to_dict("records")
        rows.sort(key=lambda r: _parse_utc(r["time"]))
    except (ValueError, TypeError, KeyError):
        return 0
    found = 0
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if (_parse_utc(b["time"]) - _parse_utc(a["time"])).total_seconds() > DUP_DT_S:
                break
            if _same_event(a["time"], a["latitude"], a["longitude"],
                           b["time"], b["latitude"], b["longitude"]):
                logger.warning(
                    "Near-duplicate events in catalog: %s and %s — "
                    "run scripts/dedup_catalog.py",
                    a["event_id"], b["event_id"],
                )
                found += 1
    return found


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
        daily.append(
            {
                "year": year,
                "jday": jday,
                "day_key": day_key(year, jday),
                "events_path": ef,
                "assignments_path": af,
            }
        )

    return daily


def load_existing_catalog(catalog_path, logger):
    """Load existing catalog if present; return (DataFrame, set-of-day-keys)."""
    if not catalog_path.exists():
        return pd.DataFrame(), set()

    df = pd.read_csv(catalog_path, dtype={"event_id": str})
    # Extract day keys from event_id: ep{YYYYMMDD}-NNNN → YYYYMMDD
    existing_days = set(df["event_id"].str[2:10])
    logger.info("Existing catalog: %d events covering %d day(s)", len(df), len(existing_days))
    return df, existing_days


def load_existing_assignments(assignments_path, logger):
    """Load existing assignments if present."""
    if not assignments_path.exists():
        return pd.DataFrame()

    df = pd.read_csv(assignments_path, dtype={"event_id": str})
    logger.debug("Existing assignments: %d rows", len(df))
    return df


def write_reviews_sidecar(
    sidecar_path: Path,
    events: dict[str, dict],
    assignments: dict[str, list],
) -> int:
    """Append every reviewed event + its picks to an append-only JSON Lines file.

    Each line is a self-contained record with ``timestamp``, ``event_id``,
    full ``event`` row, and ``picks`` list. The file is never rewritten or
    truncated — if a future rebuild loses an event, you can grep this file
    by event_id and take the most recent record to reconstruct it.

    Returns the number of records appended.
    """
    import json

    if not events:
        return 0
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    n = 0
    # Append-only: open in 'a' mode; never read or rewrite the file here.
    with sidecar_path.open("a", encoding="utf-8") as f:
        for eid, ev in events.items():
            record = {
                "timestamp": ts,
                "event_id": eid,
                "event": _jsonable(ev),
                "picks": [_jsonable(p) for p in assignments.get(eid, [])],
            }
            f.write(json.dumps(record, default=str) + "\n")
            n += 1
        f.flush()
    return n


def _jsonable(row: dict) -> dict:
    """Convert pandas/numpy scalars to JSON-serializable Python primitives."""
    out = {}
    for k, v in row.items():
        # pandas NaN floats are not JSON; serialize as None
        if v is None or (isinstance(v, float) and v != v):
            out[k] = None
        elif hasattr(v, "item"):  # numpy scalar
            out[k] = v.item()
        else:
            out[k] = v
    return out


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

    # ── Snapshot reviewed-event metadata BEFORE rebuild wipes it ─────
    # Even in --rebuild mode we must preserve user-supplied annotations:
    # reviewed/confirmed/rejected status, manually relocated lat/lon/depth,
    # edited picks. Without this snapshot, every gap-fill (which fires
    # `catalog --rebuild` via the orchestrator's --force translation) was
    # silently obliterating hours of human review work. The snapshot is
    # keyed by event_id, so it survives any change to the row order or to
    # how step 4 generates daily events.
    preserved_events: dict[str, dict] = {}  # event_id → full catalog row
    preserved_assignments: dict[str, list] = {}  # event_id → list[pick dict]
    if args.rebuild and catalog_path.exists():
        # Two-layer protection for human review work:
        #   1. In-memory snapshot (preserved_events) — restored into the new
        #      catalog at the end of this run.
        #   2. Append-only reviews.jsonl sidecar (write_reviews_sidecar) — a
        #      crash-safe record of every reviewed event ever seen. If the
        #      catalog ever gets corrupted, you can reconstruct reviews from
        #      this file. Never rewritten, only appended.
        try:
            snap_df = pd.read_csv(catalog_path, dtype={"event_id": str})
            reviewed_mask = snap_df["review_status"].fillna("").astype(str).str.strip() != ""
            # to_dict('records') is C-level (much faster than iterrows())
            for row in snap_df[reviewed_mask].to_dict("records"):
                eid = row.get("event_id")
                if eid:
                    preserved_events[eid] = row
            if preserved_events and assignments_path.exists():
                snap_assign = pd.read_csv(assignments_path, dtype={"event_id": str})
                keep_mask = snap_assign["event_id"].isin(preserved_events)
                for row in snap_assign[keep_mask].to_dict("records"):
                    eid = row.get("event_id")
                    preserved_assignments.setdefault(eid, []).append(row)
            # Write sidecar BEFORE proceeding with destructive rebuild
            sidecar_count = write_reviews_sidecar(
                catalog_path.parent / "reviews.jsonl",
                preserved_events,
                preserved_assignments,
            )
            if preserved_events:
                logger.info(
                    "Rebuild snapshot: preserving %d reviewed event(s) "
                    "with %d total pick(s) (also appended %d records to reviews.jsonl)",
                    len(preserved_events),
                    sum(len(v) for v in preserved_assignments.values()),
                    sidecar_count,
                )
        except Exception as e:
            # FAIL FAST. Previously this was a warning, which let the rebuild
            # proceed with empty preserves and silently obliterated reviews.
            # Refuse to continue — a user has to investigate the snapshot
            # failure before deciding to lose review state.
            logger.error(
                "Cannot snapshot reviewed events before rebuild: %s. "
                "Refusing to proceed — re-run without --rebuild, or fix the "
                "snapshot error first. (Reviewed-event preservation is the "
                "whole point of this safeguard.)",
                e,
            )
            sys.exit(2)

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

    # ── Restore reviewed-event rows from snapshot ────────────────────
    # Content-aware overlay: preserves manual review work across rebuilds
    # without duplicating events whose GaMMA index changed on
    # re-association. See restore_reviewed_events() for the failure modes
    # this guards against.
    catalog, assignments = restore_reviewed_events(
        catalog, assignments, preserved_events, preserved_assignments, logger
    )

    # Sort chronologically
    catalog = catalog.sort_values("time").reset_index(drop=True)
    if not assignments.empty:
        assignments = assignments.sort_values("time").reset_index(drop=True)

    # Safety net: this class of bug (same physical event under two ids)
    # must never accumulate silently again.
    warn_near_duplicates(catalog, logger)

    # ── Write (atomic: temp + fsync + replace) ───────────────────────
    # catalog.csv is the only file containing reviewed-event state. A bare
    # to_csv() that's killed mid-write (kill -9, full disk, OOM) leaves it
    # truncated and there is no other source of truth. Use atomic_write_df.
    atomic_write_df(catalog, catalog_path, index=False)
    logger.info("Wrote catalog: %s (%d events)", catalog_path, len(catalog))

    if not assignments.empty:
        atomic_write_df(assignments, assignments_path, index=False)
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
