#!/usr/bin/env python3
"""Find (and optionally merge) duplicate events in the master catalog.

Duplicates arise when association or template matching catalogs the same
source twice (e.g. ep20260307-0001 / ep20260307-0003: identical origin time,
hypocenter, and picks). Two events are considered duplicates when BOTH their
origin times are within ``--dt`` seconds AND their epicenters are within
``--dist`` km; groups are formed transitively (connected components).

Dry-run by default: prints every duplicate group and which event would be
kept. Nothing is written without ``--apply``.

With ``--apply``:
  * catalog.csv and assignments.csv are first backed up alongside the
    originals with a ``.bak-<UTC timestamp>`` suffix
  * within each group the kept event is chosen by: confirmed review status,
    then any review timestamp, then most picks, then earliest event_id
  * the dropped events' picks are folded into the kept event (skipping picks
    identical in station/phase/time), num_picks is updated, and the dropped
    rows are removed
  * every action is appended to output/5-catalog/dedup_audit.jsonl

Groups mixing pipeline (ep*) and template-match (tm*) events are reported but
NOT merged unless ``--include-template`` is passed, since an ep/tm pair may
reflect a deliberate template-matching workflow rather than a mistake.

Usage (from project root):
    python scripts/dedup_catalog.py                # dry run, report only
    python scripts/dedup_catalog.py --apply        # merge within-prefix groups
    python scripts/dedup_catalog.py --dt 3 --dist 8   # widen the net
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_FILE = ROOT / "output" / "5-catalog" / "catalog.csv"
ASSIGNMENTS_FILE = ROOT / "output" / "5-catalog" / "assignments.csv"
AUDIT_FILE = ROOT / "output" / "5-catalog" / "dedup_audit.jsonl"

DEG2KM = 111.19


def parse_time(iso: str) -> datetime:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:  # some rows (template matches) store naive UTC times
        t = t.replace(tzinfo=timezone.utc)
    return t


def horizontal_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG2KM
    dlon = (lon2 - lon1) * DEG2KM * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def find_groups(rows: list[dict], dt_s: float, dist_km: float) -> list[list[dict]]:
    """Connected components under the (time AND distance) proximity relation."""
    events = sorted(rows, key=lambda r: r["_t"])
    parent = list(range(len(events)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(events):
        for j in range(i + 1, len(events)):
            b = events[j]
            if (b["_t"] - a["_t"]).total_seconds() > dt_s:
                break  # sorted by time; no later event can match either
            if horizontal_km(a["_lat"], a["_lon"], b["_lat"], b["_lon"]) <= dist_km:
                parent[find(i)] = find(j)

    groups: dict[int, list[dict]] = {}
    for i, ev in enumerate(events):
        groups.setdefault(find(i), []).append(ev)
    return [g for g in groups.values() if len(g) > 1]


def keeper_key(row: dict) -> tuple:
    """Sort key: best candidate first."""
    return (
        row.get("review_status", "") != "confirmed",  # confirmed first
        row.get("reviewed", "") == "",                # any review beats none
        -int(float(row.get("num_picks") or 0)),       # more picks
        row["event_id"],                              # stable: earliest ID
    )


def pick_identity(p: dict) -> tuple:
    """Identity for fold-dedup: station + phase, NOT time.

    A local event has one P and one S arrival per station; two same-phase
    picks at one station are re-picks of the same arrival from different
    review sessions (observed offsets 0.1-1.5 s), not new information.
    Folding them would double every arrival and bias any relocation, so
    the keeper's own pick wins.
    """
    return (p.get("network", ""), p.get("station", ""), p.get("phase", ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dt", type=float, default=2.0, help="max origin-time separation (s), default 2")
    ap.add_argument("--dist", type=float, default=5.0, help="max epicentral separation (km), default 5")
    ap.add_argument("--apply", action="store_true", help="write the merge (default: dry run)")
    ap.add_argument("--include-template", action="store_true",
                    help="also merge groups mixing ep* and tm* events")
    ap.add_argument("--catalog", type=Path, default=CATALOG_FILE)
    ap.add_argument("--assignments", type=Path, default=ASSIGNMENTS_FILE)
    args = ap.parse_args()

    with open(args.catalog, newline="") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    for r in rows:
        r["_t"] = parse_time(r["time"])
        r["_lat"] = float(r["latitude"])
        r["_lon"] = float(r["longitude"])

    groups = find_groups(rows, args.dt, args.dist)
    if not groups:
        print(f"No duplicate groups found ({len(rows)} events, dt<={args.dt:g}s, dist<={args.dist:g}km).")
        return 0

    print(f"{len(groups)} duplicate group(s) among {len(rows)} events "
          f"(dt<={args.dt:g}s, dist<={args.dist:g}km):\n")

    mergeable: list[tuple[dict, list[dict]]] = []  # (keeper, dropped)
    for n, group in enumerate(sorted(groups, key=lambda g: g[0]["_t"]), 1):
        group = sorted(group, key=keeper_key)
        keeper, dropped = group[0], group[1:]
        prefixes = {ev["event_id"][:2] for ev in group}
        cross = len(prefixes) > 1
        skip = cross and not args.include_template

        print(f"Group {n}{' [ep/tm mix — report only, use --include-template to merge]' if skip else ''}:")
        for ev in group:
            role = "KEEP" if ev is keeper else "drop"
            dt = (ev["_t"] - keeper["_t"]).total_seconds()
            dkm = horizontal_km(keeper["_lat"], keeper["_lon"], ev["_lat"], ev["_lon"])
            print(f"  [{role}] {ev['event_id']}  {ev['time']}  "
                  f"M{ev.get('magnitude', '?')}  picks={ev.get('num_picks', '?')}  "
                  f"status={ev.get('review_status') or '-'}/{ev.get('event_type') or '-'}  "
                  f"(dt={dt:+.2f}s, d={dkm:.2f}km)")
        print()
        if not skip:
            mergeable.append((keeper, dropped))

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to merge the groups above.")
        return 0
    if not mergeable:
        print("Nothing mergeable (all groups are ep/tm mixes and --include-template not set).")
        return 0

    # ── backups ──────────────────────────────────────────────────────────
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for src in (args.catalog, args.assignments):
        bak = src.with_name(src.name + f".bak-{stamp}")
        if bak.exists():
            sys.exit(f"Backup {bak} already exists — refusing to overwrite.")
        shutil.copy2(src, bak)
        print(f"Backed up {src.name} -> {bak.name}")

    with open(args.assignments, newline="") as f:
        a_reader = csv.DictReader(f)
        a_columns = list(a_reader.fieldnames or [])
        assignments = list(a_reader)

    drop_ids: set[str] = set()
    audit_records = []
    for keeper, dropped in mergeable:
        kid = keeper["event_id"]
        existing = {pick_identity(a) for a in assignments if a.get("event_id") == kid}
        folded = 0
        for ev in dropped:
            drop_ids.add(ev["event_id"])
            for a in assignments:
                if a.get("event_id") == ev["event_id"] and pick_identity(a) not in existing:
                    a["event_id"] = kid
                    existing.add(pick_identity(a))
                    folded += 1
        keeper["num_picks"] = str(len(existing))
        audit_records.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": "merge",
            "kept": kid,
            "dropped": [ev["event_id"] for ev in dropped],
            "picks_folded": folded,
            "dt_s": args.dt,
            "dist_km": args.dist,
        })
        print(f"Merged {[ev['event_id'] for ev in dropped]} into {kid} "
              f"({folded} pick(s) folded, num_picks now {keeper['num_picks']})")

    kept_rows = [r for r in rows if r["event_id"] not in drop_ids]
    kept_assignments = [a for a in assignments if a.get("event_id") not in drop_ids]

    with open(args.catalog, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept_rows)
    with open(args.assignments, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=a_columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept_assignments)
    with open(AUDIT_FILE, "a") as f:
        for rec in audit_records:
            f.write(json.dumps(rec) + "\n")

    print(f"\nDone: {len(rows)} -> {len(kept_rows)} events; audit appended to {AUDIT_FILE.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
