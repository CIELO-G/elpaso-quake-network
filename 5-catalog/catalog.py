#!/usr/bin/env python3
"""
catalog.py - Running seismic event catalog

Step 5/5 of the El Paso seismic processing pipeline.
Scans daily event files from step 4, merges them into a single running
catalog CSV with globally unique event IDs, and does the same for
pick assignments. The catalog is updated incrementally — re-running
appends new days without duplicating existing ones.

Two sources feed it, each tracked per day by its event-id prefix:
  ep  PhaseNet events, {input_dir}/4-events (step 4)
  po  PhaseNO events PhaseNet missed, {input_dir}/{phaseno_additions_dir}
      (3-detection/phaseno/step.py). Marked detector=PhaseNO, with the
      number of PhaseNet picks that corroborate them. A day's additions
      are ingested once, independently of its PhaseNet events, so a
      PhaseNO run that comes later (backfill, retry) is still picked up.

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

from lib.atomic_io import atomic_write_df
from lib.catalog_lock import CatalogLockTimeout, catalog_lock
from lib.config import load_config, load_stations
from lib.event_ids import mint_event_id
from lib.ingested_days import (
    INGESTED_DAYS_FILENAME,
    PRIMARY_SOURCE,
    days_from_ids,
    read_ingested_days,
    write_ingested_days,
)
from lib.regional import RegionalCatalog
from lib.regional import refresh as refresh_regional
from lib.reviews import SIDECAR_FILENAME, append_review_records, jsonable
from lib.triage import TriageConfig, apply_triage, check_core_stations
from lib.logger import setup_logging

# Max seconds to wait for another catalog writer (dashboard save, dedup script).
CATALOG_LOCK_TIMEOUT_S = 120.0

DEFAULTS = {
    "stations_file": "stations.json",
    "input_dir": "output",
    "output_dir": "output",
    "catalog_file": "5-catalog/catalog.csv",
    "assignments_file": "5-catalog/assignments.csv",
    "regional_cache_file": "5-catalog/comcat_regional.csv",
    "phaseno_additions_dir": "phaseno/4-additions",
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
    parser.add_argument(
        "--retriage",
        action="store_true",
        help="Recompute triage labels on the existing catalog and exit "
             "(no new days ingested; review_status untouched)",
    )
    return parser.parse_args()


def _jday_to_date(year, jday):
    """Convert year + julian day to a YYYYMMDD string."""
    dt = datetime.date(int(year), 1, 1) + datetime.timedelta(days=int(jday) - 1)
    return dt.strftime("%Y%m%d")


# Event ids: lib/event_ids.py. A rebuild hands every re-found event its old id
# (carry_over_ids below), so reviews, NLLoc work and public-site links survive
# re-running association.


# Second-detector source (see module docstring).
PHASENO_PREFIX = "po"
PHASENO_DETECTOR = "PhaseNO"


# ── Event-identity threshold (time-only) ──────────────────────────────
# Two catalog rows with origin times within RECLAIM_DT_S are treated as the
# same physical event. Deliberately NO distance test: manual/NLLoc review
# moves epicenters 5-53 km (median ~15 km, measured 2026-07-05) from the
# GaMMA initial locations, so any spatial threshold either misses reviewed
# events' raw twins or is so wide it's meaningless. Time alone is safe here
# because association clusters picks with dbscan_eps=60 s — a single run
# cannot emit two separate events this close together, and origin-time
# shifts from relocation stay under ~5 s.
# (scripts/dedup_catalog.py and the dashboard save guard keep a stricter
# ±2 s + 5 km test — they compare like-for-like solutions.)
RECLAIM_DT_S = 10.0


def _parse_utc(iso):
    """ISO string → aware UTC datetime (tolerates 'Z'-suffixed and naive)."""
    t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t


def _same_origin(time_a, time_b):
    """Identity test: origin times within RECLAIM_DT_S seconds."""
    try:
        dt = abs((_parse_utc(time_a) - _parse_utc(time_b)).total_seconds())
    except (ValueError, TypeError):
        return False
    return dt <= RECLAIM_DT_S


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

    Identity is therefore verified by origin time (±RECLAIM_DT_S s,
    time-only — see the constant's comment for why no distance test)
    before either restore path, and an unreviewed fresh twin of a
    reclaimed reviewed row is dropped rather than duplicated.

    Restored rows never collide with fresh ids: a reviewed event whose old
    id was taken over keeps its twin's id (its identity in the current
    generation), or — with no twin — gets the day's next free suffix. Its
    picks are re-keyed to match.
    """
    if not preserved_events:
        return catalog, assignments

    for col in ("reviewed", "review_status", "event_type"):
        if col not in catalog.columns:
            catalog[col] = ""

    restored_present = 0
    reclaimed = 0
    restored_orphan = 0
    drop_row_ids: set = set()      # fresh catalog rows to remove (reclaimed twins)
    drop_assign_ids: set = set()   # fresh assignment rows to replace with snapshot picks
    used_ids: set = set(catalog["event_id"].astype(str))
    append_rows = []
    append_picks = []

    def _next_free_id(old_id):
        """A free id for a reviewed row whose id now names another event."""
        day, sep, num = old_id.rpartition("-")
        if sep and len(day) == 10 and len(num) == 4 and num.isdigit():
            # Positional (pre-2026-10-16) id: next unused NNNN of that day.
            taken = [int(u.rsplit("-", 1)[1]) for u in used_ids
                     if u.startswith(day + "-") and u.rsplit("-", 1)[1].isdigit()]
            return f"{day}-{(max(taken) + 1 if taken else 0):04d}"
        n = 2
        while f"{old_id}-{n}" in used_ids:
            n += 1
        return f"{old_id}-{n}"

    for eid, row_data in preserved_events.items():
        id_taken = False
        mask = catalog["event_id"].astype(str) == eid
        if mask.any():
            current = catalog[mask].iloc[0]
            if _same_origin(row_data.get("time"), current.get("time")):
                for col, val in row_data.items():
                    if col in catalog.columns:
                        catalog.loc[mask, col] = val
                restored_present += 1
                drop_assign_ids.add(eid)
                append_picks.extend(preserved_assignments.get(eid, []))
                continue
            id_taken = True
            logger.warning(
                "Event id %s now belongs to a different event after "
                "re-association (preserved t=%s vs new t=%s) — keeping the "
                "new event, restoring the reviewed row under a new id",
                eid, row_data.get("time"), current.get("time"),
            )
        # Orphan path: the reviewed row has no (content-matching) id in the
        # new generation. If its fresh twin exists under a new id, drop the
        # twin — the reviewed row IS that event.
        twin = None
        for _, cand in catalog.iterrows():  # O(n·m); fine at catalog scale
            cid = str(cand.get("event_id"))
            if cid == eid or cid in drop_row_ids:
                continue
            if str(cand.get("review_status") or "").strip():
                continue  # never silently drop another reviewed row
            if _same_origin(row_data.get("time"), cand.get("time")):
                twin = cid
                break
        if twin is not None:
            drop_row_ids.add(twin)
            drop_assign_ids.add(twin)
            reclaimed += 1
            # If the old id now belongs to a different event, the reviewed
            # row adopts its twin's id — its identity in the current
            # generation, stable across future rebuilds.
            out_id = twin if id_taken else eid
            logger.info("Reviewed event %s reclaimed its re-associated twin %s",
                        eid, twin)
        else:
            restored_orphan += 1
            out_id = _next_free_id(eid) if id_taken else eid
        if out_id != eid:
            logger.info("Reviewed event %s restored as %s", eid, out_id)
        used_ids.add(out_id)
        row = {col: row_data.get(col, "") for col in catalog.columns}
        row["event_id"] = out_id
        append_rows.append(row)
        for pick in preserved_assignments.get(eid, []):
            append_picks.append({**pick, "event_id": out_id})

    if drop_row_ids:
        catalog = catalog[~catalog["event_id"].astype(str).isin(drop_row_ids)]
        catalog = catalog.reset_index(drop=True)
    if append_rows:
        catalog = pd.concat([catalog, pd.DataFrame(append_rows)], ignore_index=True)

    # Replace the affected events' fresh assignment rows with snapshot picks.
    # Per-event (not blanket by snapshot key): a stolen id's fresh picks
    # belong to the NEW event and must survive.
    if not assignments.empty and drop_assign_ids:
        keep_mask = ~assignments["event_id"].astype(str).isin(drop_assign_ids)
        assignments = assignments[keep_mask].reset_index(drop=True)
    if append_picks:
        picks_df = pd.DataFrame(append_picks)
        assignments = (
            pd.concat([assignments, picks_df], ignore_index=True)
            if not assignments.empty
            else picks_df
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
        rows = catalog[["event_id", "time"]].to_dict("records")
        rows.sort(key=lambda r: _parse_utc(r["time"]))
    except (ValueError, TypeError, KeyError):
        return 0
    found = 0
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if not _same_origin(a["time"], b["time"]):
                break  # rows are time-sorted
            logger.warning(
                "Near-duplicate events in catalog: %s and %s (origins within "
                "%gs) — run scripts/dedup_catalog.py",
                a["event_id"], b["event_id"], RECLAIM_DT_S,
            )
            found += 1
    return found


def carry_over_ids(catalog, assignments, previous, logger):
    """Give each freshly read event the id of the previous event it re-finds.

    `previous` holds event_id and time of the catalog before a rebuild. A
    fresh and a previous event are the same when they come from the same
    source (2-character id prefix) and their origins are within RECLAIM_DT_S;
    pairs are matched one-to-one, closest first. Unmatched fresh events keep
    their newly minted ids; unmatched previous ones are gone (restored later
    only if reviewed). Fresh ids must not collide with previous ones (mint
    them with the previous ids in `taken`). Returns (catalog, assignments).
    """
    if catalog.empty or previous is None or previous.empty:
        return catalog, assignments

    def _seconds(times):
        t = pd.to_datetime(times, utc=True, format="ISO8601", errors="coerce")
        return (t - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()

    fresh = pd.DataFrame({"id": catalog["event_id"].astype(str).to_numpy(),
                          "t": _seconds(catalog["time"]).to_numpy()})
    old = pd.DataFrame({"id": previous["event_id"].astype(str).to_numpy(),
                        "t": _seconds(previous["time"]).to_numpy()}).dropna()
    pairs = []
    for src, olds in old.groupby(old["id"].str[:2]):
        olds = olds.sort_values("t")
        ot = olds["t"].to_numpy()
        for fid, ft in fresh[fresh["id"].str[:2] == src].itertuples(index=False):
            if pd.isna(ft):
                continue
            lo = ot.searchsorted(ft - RECLAIM_DT_S, side="left")
            hi = ot.searchsorted(ft + RECLAIM_DT_S, side="right")
            for oid, o_t in olds.iloc[lo:hi].itertuples(index=False):
                pairs.append((abs(ft - o_t), fid, oid))
    rename, used_old = {}, set()
    for _, fid, oid in sorted(pairs):
        if fid not in rename and oid not in used_old:
            rename[fid] = oid
            used_old.add(oid)
    rename = {f: o for f, o in rename.items() if f != o}
    if rename:
        catalog = catalog.assign(event_id=catalog["event_id"].replace(rename))
        if not assignments.empty:
            assignments = assignments.assign(event_id=assignments["event_id"].replace(rename))
    logger.info(
        "Event ids: %d re-found event(s) kept their id, %d new",
        len(used_old), len(fresh) - len(used_old),
    )
    return catalog, assignments


def day_key(year, jday):
    """Return the YYYYMMDD string used to track which days are cataloged."""
    return _jday_to_date(year, jday)


def discover_daily_files(input_dir, logger, subdir="4-events"):
    """Find all daily events/assignments CSV pairs under {input_dir}/{subdir}/."""
    events_dir = Path(input_dir) / subdir
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


def earliest_origin(catalog_path: Path, daily_files: list[dict]) -> pd.Timestamp | None:
    """Earliest origin this run will triage: the existing catalog or a new day."""
    candidates = [pd.Timestamp(d["day_key"], tz="UTC") for d in daily_files]
    if catalog_path.exists():
        times = pd.read_csv(catalog_path, usecols=["time"])["time"].dropna()
        if not times.empty:
            candidates.append(pd.to_datetime(times, utc=True, format="ISO8601").min())
    return min(candidates) if candidates else None


def load_regional(
    triage_cfg: TriageConfig, cache_path: Path, need_from: pd.Timestamp | None, logger
) -> RegionalCatalog | None:
    """Refresh the ComCat cache for the regional rule; None when the rule is off.

    Call this before taking the catalog lock: it waits on the network, and
    reviewer saves in the dashboard must not.
    """
    if not (triage_cfg.enabled and triage_cfg.regional.enabled) or need_from is None:
        return None
    regional = refresh_regional(cache_path, triage_cfg.regional, need_from, logger=logger)
    if regional.covered is None:
        logger.warning("Regional triage rule enabled but no ComCat events cached; rule skipped")
    return regional


def load_existing_catalog(catalog_path, logger):
    """Load existing catalog if present; return (DataFrame, set-of-day-keys)."""
    if not catalog_path.exists():
        return pd.DataFrame(), set()

    df = pd.read_csv(catalog_path, dtype={"event_id": str})
    existing_days = cataloged_days(df)
    logger.info("Existing catalog: %d events covering %d day(s)", len(df), len(existing_days))
    return df, existing_days


def cataloged_days(catalog, prefix=None):
    """YYYYMMDD keys of the days a source already has in the catalog.

    prefix=None is the primary source: every id except the PhaseNO ones
    (template-matching "tm" rows included, as before). Read off the ids, so
    exact only for positional ids ({prefix}{YYYYMMDD}-NNNN); the catalog step
    itself uses the ingested_days.json ledger (lib/ingested_days.py).
    """
    if catalog.empty:
        return set()
    days = days_from_ids(catalog["event_id"].astype(str))
    return days[prefix or PRIMARY_SOURCE]


def load_existing_assignments(assignments_path, logger):
    """Load existing assignments if present."""
    if not assignments_path.exists():
        return pd.DataFrame()

    df = pd.read_csv(assignments_path, dtype={"event_id": str, "network": str, "station": str, "location": str, "channel": str}, keep_default_na=False)
    logger.debug("Existing assignments: %d rows", len(df))
    return df


def write_reviews_sidecar(
    sidecar_path: Path,
    events: dict[str, dict],
    assignments: dict[str, list],
    *,
    source: str = "rebuild",
) -> int:
    """Append reviewed events + picks to the append-only sidecar.

    Thin wrapper over :func:`lib.reviews.append_review_records` kept for the
    existing call sites and tests. Returns the number of records appended.
    """
    return append_review_records(Path(sidecar_path), events, assignments, source=source)


_jsonable = jsonable


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
    regional_cache = output_dir / config["regional_cache_file"]
    triage_cfg = TriageConfig.from_config(config.get("triage"))
    for warning in check_core_stations(triage_cfg, stations):
        logger.warning(warning)

    if getattr(args, "retriage", False):
        # Relabel the existing catalog in place. Used for the initial
        # backfill and after changing triage thresholds.
        regional = load_regional(
            triage_cfg, regional_cache, earliest_origin(catalog_path, []), logger
        )
        try:
            with catalog_lock(catalog_path.parent, timeout=CATALOG_LOCK_TIMEOUT_S):
                _retriage_under_lock(
                    catalog_path, assignments_path, triage_cfg, regional, logger
                )
        except CatalogLockTimeout as e:
            logger.error("%s — catalog not retriaged.", e)
            sys.exit(3)
        return

    # Ensure output directory exists
    catalog_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Discover daily files ─────────────────────────────────────────
    daily_files = discover_daily_files(input_dir, logger)
    if not daily_files:
        logger.warning("No daily event files found — nothing to catalog")
        return

    logger.info("Discovered %d day(s) with event files", len(daily_files))
    additions_dir = config.get("phaseno_additions_dir")
    addition_files = (
        discover_daily_files(input_dir, logger, subdir=additions_dir) if additions_dir else []
    )
    if addition_files:
        logger.info("Discovered %d day(s) with PhaseNO additions", len(addition_files))
    sources = [
        (None, daily_files),
        (PHASENO_PREFIX, addition_files),
    ]
    regional = load_regional(
        triage_cfg, regional_cache, earliest_origin(catalog_path, daily_files), logger
    )

    # The merge below is a whole-file read-modify-write of catalog.csv and
    # assignments.csv. The dashboard (reviewer saves) and maintenance scripts
    # do the same, from other processes, so hold the shared flock for the
    # entire read→merge→write window. A reviewer's save never lands inside it.
    try:
        with catalog_lock(catalog_path.parent, timeout=CATALOG_LOCK_TIMEOUT_S):
            _merge_under_lock(
                args, sources, catalog_path, assignments_path, triage_cfg, regional, logger
            )
    except CatalogLockTimeout as e:
        logger.error("%s — leaving catalog untouched; the orchestrator will retry.", e)
        sys.exit(3)


def _retriage_under_lock(catalog_path, assignments_path, triage_cfg, regional, logger):
    """Recompute triage labels on the existing catalog (read-modify-write)."""
    existing_catalog, _ = load_existing_catalog(catalog_path, logger)
    existing_assignments = load_existing_assignments(assignments_path, logger)
    if existing_catalog.empty:
        logger.warning("No catalog to retriage")
        return
    relabelled = apply_triage(existing_catalog, existing_assignments, triage_cfg, regional)
    atomic_write_df(relabelled, catalog_path, index=False)
    counts = relabelled["triage"].value_counts().to_dict()
    logger.info("Retriaged %d events: %s", len(relabelled), counts)


def _read_day(day, prefix, logger, taken):
    """One day file pair -> (events, assignments) with global ids, or None.

    prefix None = PhaseNet ("ep"); PHASENO_PREFIX additionally marks the
    rows detector=PhaseNO (their corroborating_picks column comes along).
    """
    events_path = day["events_path"]
    try:
        events = pd.read_csv(events_path, dtype={"event_id": str})
    except Exception as e:
        logger.warning("Failed to read %s: %s", events_path, e)
        return None
    if events.empty:
        return None

    id_prefix = prefix or "ep"
    events["event_id"] = [mint_event_id(t, id_prefix, taken) for t in events["time"]]
    if prefix == PHASENO_PREFIX:
        events["detector"] = PHASENO_DETECTOR
    events = events[["event_id"] + [c for c in events.columns if c != "event_id"]]

    assigns = pd.DataFrame()
    assign_path = day["assignments_path"]
    if assign_path.exists():
        try:
            assigns = pd.read_csv(assign_path, dtype={"network": str, "station": str, "location": str, "channel": str}, keep_default_na=False)
        except Exception as e:
            logger.warning("Failed to read %s: %s", assign_path, e)
            assigns = pd.DataFrame()
    if not assigns.empty:
        idx_to_id = dict(zip(events["event_index"], events["event_id"]))
        assigns["event_id"] = assigns["event_index"].map(idx_to_id)
        assigns = assigns.drop(columns=["event_index"])
        assigns = assigns[["event_id"] + [c for c in assigns.columns if c != "event_id"]]
    return events, assigns


def _merge_under_lock(
    args, sources, catalog_path, assignments_path, triage_cfg, regional, logger
):
    """Snapshot → load → merge new days → restore reviews → triage → write.

    `sources` is [(prefix, daily_files)]: prefix None for PhaseNet's 4-events,
    PHASENO_PREFIX for PhaseNO's additions. Each source is ingested for the
    days it does not have in the catalog yet.
    """
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
    previous = pd.DataFrame()  # event_id + time of every row before a rebuild
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
            previous = snap_df[["event_id", "time"]]
            reviewed_mask = snap_df["review_status"].fillna("").astype(str).str.strip() != ""
            # to_dict('records') is C-level (much faster than iterrows())
            for row in snap_df[reviewed_mask].to_dict("records"):
                eid = row.get("event_id")
                if eid:
                    preserved_events[eid] = row
            if preserved_events and assignments_path.exists():
                snap_assign = pd.read_csv(assignments_path, dtype={"event_id": str, "network": str, "station": str, "location": str, "channel": str}, keep_default_na=False)
                keep_mask = snap_assign["event_id"].isin(preserved_events)
                for row in snap_assign[keep_mask].to_dict("records"):
                    eid = row.get("event_id")
                    preserved_assignments.setdefault(eid, []).append(row)
            # Write sidecar BEFORE proceeding with destructive rebuild
            sidecar_count = write_reviews_sidecar(
                catalog_path.parent / SIDECAR_FILENAME,
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
    else:
        existing_catalog, _ = load_existing_catalog(catalog_path, logger)
        existing_assignments = load_existing_assignments(assignments_path, logger)

    # ── Process new days ─────────────────────────────────────────────
    # New ids must not reuse any id the catalog has (or had, before a rebuild).
    taken = set(existing_catalog["event_id"].astype(str)) if not existing_catalog.empty else set()
    if not previous.empty:
        taken |= set(previous["event_id"].astype(str))
    new_event_dfs = []
    new_assign_dfs = []
    new_day_count = 0

    # Days each source has ingested. A rebuild re-ingests everything.
    ledger_path = catalog_path.parent / INGESTED_DAYS_FILENAME
    ingested = {} if args.rebuild else read_ingested_days(ledger_path)
    if ingested is None:
        logger.info("No %s yet: taking ingested days from the catalog's ids", ledger_path.name)
        ingested = {
            prefix or PRIMARY_SOURCE: cataloged_days(existing_catalog, prefix)
            for prefix, _ in sources
        }

    for prefix, daily_files in sources:
        existing_days = ingested.setdefault(prefix or PRIMARY_SOURCE, set())
        for day in daily_files:
            dk = day["day_key"]
            if dk in existing_days:
                logger.debug("Day %s already in catalog (%s), skipping", dk, prefix or "ep")
                continue
            got = _read_day(day, prefix, logger, taken)
            if got is None:
                logger.debug("Day %s has no %s events, skipping", dk, prefix or "ep")
                continue
            events, assigns = got
            new_event_dfs.append(events)
            if not assigns.empty:
                new_assign_dfs.append(assigns)
            logger.debug("Day %s: %d %s event(s)", dk, len(events), prefix or "ep")
            new_day_count += 1
            existing_days.add(dk)

    # ── Merge ────────────────────────────────────────────────────────
    if not new_event_dfs:
        if not args.rebuild and not ledger_path.exists():
            write_ingested_days(ledger_path, ingested)
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

    # ── Keep ids across a rebuild ────────────────────────────────────
    if args.rebuild:
        catalog, assignments = carry_over_ids(catalog, assignments, previous, logger)

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

    # Triage labels (never touches review_status). Recomputed for every
    # row on each run so threshold changes take effect immediately.
    catalog = apply_triage(catalog, assignments, triage_cfg, regional)
    if triage_cfg.enabled:
        logger.info("Triage: %s", catalog["triage"].value_counts().to_dict())

    # ── Write (atomic: temp + fsync + replace) ───────────────────────
    # catalog.csv is the only file containing reviewed-event state. A bare
    # to_csv() that's killed mid-write (kill -9, full disk, OOM) leaves it
    # truncated and there is no other source of truth. Use atomic_write_df.
    atomic_write_df(catalog, catalog_path, index=False)
    logger.info("Wrote catalog: %s (%d events)", catalog_path, len(catalog))

    if not assignments.empty:
        atomic_write_df(assignments, assignments_path, index=False)
        logger.info("Wrote assignments: %s (%d rows)", assignments_path, len(assignments))
    # After the catalog: a crash in between re-ingests the days (duplicates,
    # which warn_near_duplicates reports) rather than losing them.
    write_ingested_days(ledger_path, ingested)

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
