"""Three-way locator comparison: GaMMA (catalog) vs GridSearch vs NLLoc.

Usage
-----
    python scripts/validate_locator.py ep20251022-0001

Loads the specified event from the catalog and the associated picks from
the day's assignments file, runs both the in-house GridSearch locator and
NLLoc on the same pick set, and prints a side-by-side comparison with
the catalog (GaMMA) entry.

NLLoc rows are skipped silently if the NLLoc binary or precomputed
travel-time grids aren't available — see ``nlloc/README.md`` for
one-time setup.

No pipeline state is modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.location import (  # noqa: E402
    DEFAULT_WEST_TEXAS_MODEL,
    GridSearchLocator,
    Pick,
    Station,
)
from lib.projection import latlon_to_km  # noqa: E402

CATALOG_PATH = ROOT / "output" / "5-catalog" / "catalog.csv"
EVENTS_DIR = ROOT / "output" / "4-events"
SAME_EVENT = timedelta(seconds=10)  # relocation moves an origin by up to ~5 s
STATIONS_PATH = ROOT / "stations.json"


def load_catalog_row(event_id: str) -> dict:
    with CATALOG_PATH.open() as f:
        for row in csv.DictReader(f):
            if row["event_id"] == event_id:
                return row
    raise SystemExit(f"event {event_id} not found in {CATALOG_PATH}")


def utc(iso: str) -> datetime:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def find_event_day(cat: dict) -> Path:
    """Directory of the association day that holds this catalog event.

    Usually the origin's UTC day, but an event within seconds of midnight can
    sit in the neighbouring day's file, so the day is the one whose event
    with this event_index has (about) this origin time.
    """
    origin, index = utc(cat["time"]), int(float(cat["event_index"]))
    for shift in (0, 1, -1):
        day = origin + timedelta(days=shift)
        year, jday = f"{day.year}", f"{day.timetuple().tm_yday:03d}"
        events = EVENTS_DIR / year / jday / f"{year}.{jday}.events.csv"
        if not events.exists():
            continue
        with events.open() as f:
            for row in csv.DictReader(f):
                if int(row["event_index"]) == index and abs(utc(row["time"]) - origin) <= SAME_EVENT:
                    return events.parent
    raise SystemExit(f"no association day holds event_index={index} at {cat['time']}")


def load_event_day_picks(cat: dict) -> tuple[list[dict], Path]:
    """Load all picks assigned to this event on its pipeline day."""
    event_index = int(float(cat["event_index"]))  # pandas may write "14.0"
    day_dir = find_event_day(cat)
    assignments = day_dir / f"{day_dir.parent.name}.{day_dir.name}.assignments.csv"
    if not assignments.exists():
        raise SystemExit(f"assignments file not found: {assignments}")
    rows: list[dict] = []
    with assignments.open() as f:
        for row in csv.DictReader(f):
            if int(row["event_index"]) == event_index:
                rows.append(row)
    if not rows:
        raise SystemExit(f"no picks for event_index={event_index} in {assignments}")
    return rows, assignments


def load_stations() -> list[Station]:
    with STATIONS_PATH.open() as f:
        raw = json.load(f)
    stations: list[Station] = []
    for entry in raw:
        sid = f"{entry['network']}.{entry['station']}"
        stations.append(
            Station(
                station_id=sid,
                latitude=float(entry["latitude"]),
                longitude=float(entry["longitude"]),
                elevation_m=float(entry.get("elevation_m", 0.0) or 0.0),
            )
        )
    return stations


def iso_to_seconds(iso: str, reference_epoch: datetime) -> float:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - reference_epoch).total_seconds()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("event_id")
    ap.add_argument("--h-half-km", type=float, default=40.0)
    ap.add_argument("--h-spacing-km", type=float, default=0.25)
    ap.add_argument("--depth-max-km", type=float, default=25.0)
    ap.add_argument("--depth-spacing-km", type=float, default=0.25)
    args = ap.parse_args()

    cat = load_catalog_row(args.event_id)
    pick_rows, assignments_path = load_event_day_picks(cat)

    stations = load_stations()
    known_ids = {s.station_id for s in stations}

    # Reference epoch: event origin time from catalog (numerically convenient).
    ref_epoch = datetime.fromisoformat(cat["time"].replace("Z", "+00:00"))
    if ref_epoch.tzinfo is None:
        ref_epoch = ref_epoch.replace(tzinfo=timezone.utc)

    picks: list[Pick] = []
    skipped_unknown: set[str] = set()
    for r in pick_rows:
        sid = f"{r['network']}.{r['station']}"
        if sid not in known_ids:
            skipped_unknown.add(sid)
            continue
        picks.append(
            Pick(
                station_id=sid,
                phase=r["phase"],
                time_s=iso_to_seconds(r["time"], ref_epoch),
            )
        )

    if skipped_unknown:
        print(f"  (ignored picks from unknown stations: {sorted(skipped_unknown)})")

    print(f"Event:          {args.event_id}")
    print(f"Assignments:    {assignments_path.relative_to(ROOT)}")
    print(f"Picks loaded:   {len(picks)}")

    cat_lat = float(cat["latitude"])
    cat_lon = float(cat["longitude"])
    cat_depth = float(cat["depth_km"])

    # ── GridSearch ───────────────────────────────────────────────
    gs = GridSearchLocator(stations, model=DEFAULT_WEST_TEXAS_MODEL)
    cx, cy = latlon_to_km(cat_lat, cat_lon, proj=gs._proj)
    gs_result = gs.locate(
        picks,
        x_half_width_km=args.h_half_km,
        y_half_width_km=args.h_half_km,
        horizontal_spacing_km=args.h_spacing_km,
        depth_min_km=0.0,
        depth_max_km=args.depth_max_km,
        depth_spacing_km=args.depth_spacing_km,
        center_xy_km=(float(cx), float(cy)),
    )

    # ── NLLoc (skip on missing install/grids) ────────────────────
    nl_result = None
    nl_skip_reason = None
    try:
        from lib.location.nlloc import NLLocLocator  # noqa: E402

        nl = NLLocLocator(stations, require_all_grids=True)
        nl_result = nl.locate(picks, ref_epoch_unix=ref_epoch.timestamp(), min_picks=4)
    except FileNotFoundError as exc:
        nl_skip_reason = f"NLLoc unavailable: {exc}"
    except Exception as exc:
        nl_skip_reason = f"NLLoc failed: {type(exc).__name__}: {exc}"

    # ── Report ───────────────────────────────────────────────────
    import math

    def _h_offset_km(lat: float, lon: float) -> float:
        dlat_km = (lat - cat_lat) * 111.32
        dlon_km = (lon - cat_lon) * 111.32 * math.cos(math.radians(cat_lat))
        return math.sqrt(dlat_km**2 + dlon_km**2)

    headers = ["", "GaMMA (catalog)", "GridSearch", "NLLoc"]
    print()
    print(f"{headers[0]:<14}{headers[1]:>22}{headers[2]:>16}{headers[3]:>16}")
    print("-" * 68)

    def _row(label: str, fmt: str, *vals) -> None:
        parts = [f"{label:<14}"]
        for v in vals:
            parts.append(fmt.format(v) if v is not None else f"{'—':>16}")
        # First column is wider than the rest
        print(parts[0] + f"{parts[1]:>22}" + "".join(f"{p:>16}" for p in parts[2:]))

    _row(
        "latitude", "{:.5f}", cat_lat, gs_result.latitude, nl_result.latitude if nl_result else None
    )
    _row(
        "longitude",
        "{:.5f}",
        cat_lon,
        gs_result.longitude,
        nl_result.longitude if nl_result else None,
    )
    _row(
        "depth (km)",
        "{:.2f}",
        cat_depth,
        gs_result.depth_km,
        nl_result.depth_km if nl_result else None,
    )
    _row(
        "horiz Δ km",
        "{:.2f}",
        0.0,
        _h_offset_km(gs_result.latitude, gs_result.longitude),
        _h_offset_km(nl_result.latitude, nl_result.longitude) if nl_result else None,
    )
    _row(
        "depth Δ km",
        "{:.2f}",
        0.0,
        gs_result.depth_km - cat_depth,
        (nl_result.depth_km - cat_depth) if nl_result else None,
    )
    _row(
        "RMS (s)",
        "{:.3f}",
        float(cat["sigma_time"]),
        gs_result.rms_residual_s,
        nl_result.rms_residual_s if nl_result else None,
    )
    _row(
        "ellipse maj (km)",
        "{:.2f}",
        None,
        gs_result.horizontal_semi_axis_major_km,
        nl_result.horizontal_semi_axis_major_km if nl_result else None,
    )
    _row(
        "ellipse min (km)",
        "{:.2f}",
        None,
        gs_result.horizontal_semi_axis_minor_km,
        nl_result.horizontal_semi_axis_minor_km if nl_result else None,
    )
    _row(
        "σz (km)",
        "{:.2f}",
        None,
        gs_result.sigma_depth_km,
        nl_result.sigma_depth_km if nl_result else None,
    )
    _row(
        "picks used",
        "{:d}",
        int(cat["num_picks"]),
        gs_result.n_picks_used,
        nl_result.n_picks_used if nl_result else None,
    )

    if nl_skip_reason:
        print(f"\n  (NLLoc column skipped: {nl_skip_reason})")

    # ── Residuals (GridSearch only; NLLoc residuals are sec-since-epoch) ─
    print()
    print("GridSearch per-pick residuals:")
    for r in sorted(gs_result.residuals, key=lambda x: (x.station_id, x.phase)):
        print(
            f"  {r.station_id:12} {r.phase}  "
            f"obs={r.observed_s:8.3f}  pred={r.predicted_s:8.3f}  "
            f"res={r.residual_s:+.3f} s"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
