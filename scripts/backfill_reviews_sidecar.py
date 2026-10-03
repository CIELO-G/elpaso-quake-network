"""Backfill reviews.jsonl from the current catalog.

Until 2026-10-03 the dashboard wrote reviewer decisions only into
``catalog.csv``; ``reviews.jsonl`` was appended only on ``catalog --rebuild``.
This one-off script appends a record for every reviewed event whose current
catalog row differs from its most recent sidecar record (or has none), so the
append-only history is complete from here on.

    python scripts/backfill_reviews_sidecar.py            # dry run
    python scripts/backfill_reviews_sidecar.py --apply

Safe to re-run: identical state is never appended twice. Takes the shared
catalog lock, so it cannot interleave with the pipeline or a reviewer save.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.catalog_lock import catalog_lock  # noqa: E402
from lib.reviews import SIDECAR_FILENAME, append_review_records, jsonable, latest_records  # noqa: E402

CATALOG_DIR = ROOT / "output" / "5-catalog"


def _norm(row: dict) -> dict:
    """Compare on string form: CSV round-trips lose numeric types."""
    return {k: ("" if v is None else str(v)) for k, v in jsonable(row).items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="append (default: report only)")
    ap.add_argument("--catalog-dir", type=Path, default=CATALOG_DIR)
    args = ap.parse_args()

    cat_path = args.catalog_dir / "catalog.csv"
    asg_path = args.catalog_dir / "assignments.csv"
    side_path = args.catalog_dir / SIDECAR_FILENAME
    if not cat_path.exists():
        print(f"no catalog at {cat_path}")
        return 1

    with catalog_lock(args.catalog_dir, timeout=60):
        cat = pd.read_csv(cat_path, dtype=str, keep_default_na=False)
        asg = pd.read_csv(asg_path, dtype=str, keep_default_na=False) if asg_path.exists() else pd.DataFrame()
        reviewed = cat[cat["review_status"].str.strip() != ""] if "review_status" in cat else cat.iloc[0:0]
        latest = latest_records(side_path)

        events: dict[str, dict] = {}
        picks: dict[str, list] = {}
        for row in reviewed.to_dict("records"):
            eid = row["event_id"]
            prev = latest.get(eid, {}).get("event")
            if prev is not None and _norm(prev) == _norm(row):
                continue
            events[eid] = row
            if not asg.empty:
                picks[eid] = asg[asg["event_id"] == eid].to_dict("records")

        print(f"reviewed events: {len(reviewed)}; already current in sidecar: "
              f"{len(reviewed) - len(events)}; to append: {len(events)}")
        for eid in list(events)[:10]:
            print("  ", eid, events[eid].get("review_status"), events[eid].get("reviewed"))
        if len(events) > 10:
            print(f"   ... {len(events) - 10} more")
        if args.apply and events:
            n = append_review_records(side_path, events, picks, source="backfill")
            print(f"appended {n} record(s) to {side_path}")
        elif events:
            print("dry run — re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
