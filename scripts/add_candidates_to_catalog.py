#!/usr/bin/env python3
"""Add template-match candidates to the catalog as unreviewed events.

Reads new_candidates.csv (produced by run_template_match.py + the looser
filter) and appends each candidate to output/5-catalog/catalog.csv with
``review_status=""`` so they show up as Unreviewed in the dashboard.

For each candidate:
  - event_id uses a ``tm`` prefix (template-match) to distinguish from
    ``ep`` (event-pipeline) events.
  - latitude/longitude/depth seed from the best-matching template's
    confirmed location — that's our best guess until you re-pick.
  - magnitude is left blank (no waveform-derived ML computed yet).
  - num_picks=0; picks will be added during review.

The user then reviews each candidate in the dashboard as normal: load
waveforms, add picks, relocate, confirm or reject.

Usage
-----
    python scripts/add_candidates_to_catalog.py
    python scripts/add_candidates_to_catalog.py --dry-run
    python scripts/add_candidates_to_catalog.py \\
        --candidates output/templates/new_candidates.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.atomic_io import atomic_write_df

CATALOG_PATH = ROOT / "output" / "5-catalog" / "catalog.csv"
DEFAULT_CANDIDATES = ROOT / "output" / "templates" / "new_candidates.csv"


def next_event_id(catalog: pd.DataFrame, event_time: str, taken: set[str]) -> str:
    """Return a fresh tm<YYYYMMDD>-<NNNN> id.

    Avoids both catalog rows AND ids already allocated in the current batch
    (``taken``) so multiple candidates on the same day get distinct ids.
    """
    day = event_time[:10].replace("-", "")
    stem = f"tm{day}"
    existing = {
        e
        for e in catalog["event_id"].dropna().astype(str).tolist()
        if e.startswith(stem)
    } | taken
    n = 0
    while f"{stem}-{n:04d}" in existing:
        n += 1
    return f"{stem}-{n:04d}"


def lookup_template_location(catalog: pd.DataFrame, template_id: str) -> dict:
    """Return lat/lon/depth/event_type from the template's row, or empty dict."""
    matches = catalog[catalog["event_id"] == template_id]
    if matches.empty:
        return {}
    row = matches.iloc[0]
    return {
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "depth_km": row.get("depth_km"),
        "event_type": row.get("event_type", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_CANDIDATES,
        help=f"CSV of candidates (default: {DEFAULT_CANDIDATES.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be added without modifying the catalog",
    )
    args = parser.parse_args()

    if not args.candidates.exists():
        print(f"ERROR: candidates file not found: {args.candidates}", file=sys.stderr)
        return 1
    if not CATALOG_PATH.exists():
        print(f"ERROR: catalog not found: {CATALOG_PATH}", file=sys.stderr)
        return 1

    candidates = pd.read_csv(args.candidates)
    catalog = pd.read_csv(CATALOG_PATH, dtype={"event_id": str})

    print(f"Catalog: {len(catalog)} existing events")
    print(f"Candidates to add: {len(candidates)}")
    print()

    new_rows = []
    skipped = []
    allocated_ids: set[str] = set()
    for _, c in candidates.iterrows():
        t = str(c["time"])
        # Match the catalog's time format if it's missing a 'Z' suffix
        if not t.endswith("Z") and "T" in t:
            t = t + "Z" if "+" not in t and "-" not in t.split("T")[1] else t

        # Skip if any catalog row is already within ±5s of this time
        # (defensive — shouldn't happen since these came from the
        # "NEW candidates" filter, but cheap to double-check)
        candidate_dt = pd.Timestamp(t).tz_localize(None)
        catalog["_t_check"] = pd.to_datetime(
            catalog["time"], errors="coerce"
        ).dt.tz_localize(None)
        close = (catalog["_t_check"] - candidate_dt).abs().dt.total_seconds() <= 5
        catalog = catalog.drop(columns=["_t_check"])
        if close.any():
            existing_id = catalog.loc[close, "event_id"].iloc[0]
            skipped.append((t, f"within 5s of existing event {existing_id}"))
            continue

        eid = next_event_id(catalog, t, allocated_ids)
        allocated_ids.add(eid)
        loc = lookup_template_location(catalog, str(c["best_template"]))
        row = {
            "event_id": eid,
            "event_index": "",
            "time": t,
            "magnitude": "",
            "magnitude_type": "",
            "ml_err": "",
            "latitude": loc.get("latitude", ""),
            "longitude": loc.get("longitude", ""),
            "depth_km": loc.get("depth_km", ""),
            "sigma_time": "",
            "sigma_amp": "",
            "num_picks": 0,
            "num_ml_sta": "",
            "reviewed": "",
            "review_status": "",  # blank → shows as Unreviewed
            "event_type": "",  # leave blank so user picks during review
        }
        new_rows.append(row)
        print(
            f"  {eid}  {t[:23]}  loc=({loc.get('latitude')}, {loc.get('longitude')})"
            f"  seeded from template {c['best_template']}"
        )

    print()
    if skipped:
        print(f"Skipped {len(skipped)} candidates:")
        for t, why in skipped:
            print(f"  {t[:23]}  — {why}")
        print()

    if not new_rows:
        print("No new rows to add.")
        return 0

    print(f"Will append {len(new_rows)} row(s).")
    if args.dry_run:
        print("(--dry-run: not writing catalog)")
        return 0

    new_df = pd.DataFrame(new_rows)
    out = pd.concat([catalog, new_df], ignore_index=True)
    # Keep chronological
    out = out.sort_values("time").reset_index(drop=True)

    atomic_write_df(out, CATALOG_PATH, index=False)
    print(f"\nWrote {CATALOG_PATH.relative_to(ROOT)} ({len(out)} events)")
    print("\nNew events will show as 'Unreviewed' in the dashboard catalog tab.")
    print("Review them as you would any other event.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
