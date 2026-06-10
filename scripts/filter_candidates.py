#!/usr/bin/env python3
"""Filter raw template-match detections into clean candidate events.

The matched filter (run_template_match.py) writes 100k+ raw detections at
permissive thresholds — that's by design, so we don't miss anything. This
script does the post-processing that turns that noise into reviewable
candidates: thresholding by per-channel correlation, time-clustering
simultaneous hits across templates, and excluding events that already
exist in the catalog.

Usage
-----
    # Defaults: per-channel >=0.5 AND (>=2 templates OR per-channel >=0.65)
    python scripts/filter_candidates.py

    # Stricter (high-confidence only)
    python scripts/filter_candidates.py --min-per-chan 0.7 --min-templates 3

    # Looser (more candidates, more noise)
    python scripts/filter_candidates.py --min-per-chan 0.4 --min-templates 1

    # Tighter time-clustering window (default 5s)
    python scripts/filter_candidates.py --cluster-window 3

Inputs
------
    output/templates/detections.csv     (from run_template_match.py)
    output/5-catalog/catalog.csv        (used to exclude known events)

Outputs
-------
    output/templates/new_candidates.csv (clean candidate event list)
    output/templates/filter_summary.json (provenance: params + counts)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from obspy import UTCDateTime

ROOT = Path(__file__).resolve().parent.parent
DETECTIONS_PATH = ROOT / "output" / "templates" / "detections.csv"
CATALOG_PATH = ROOT / "output" / "5-catalog" / "catalog.csv"
OUT_CANDIDATES = ROOT / "output" / "templates" / "new_candidates.csv"
OUT_SUMMARY = ROOT / "output" / "templates" / "filter_summary.json"


def load_confirmed_event_times() -> list[UTCDateTime]:
    """Return UTCDateTimes of every confirmed event in the catalog."""
    if not CATALOG_PATH.exists():
        return []
    times = []
    with CATALOG_PATH.open() as f:
        for row in csv.DictReader(f):
            if row.get("review_status") == "confirmed":
                try:
                    times.append(UTCDateTime(row["time"]))
                except Exception:
                    continue
    return times


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--detections", type=Path, default=DETECTIONS_PATH,
        help=f"Raw detections CSV (default: {DETECTIONS_PATH.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--out", type=Path, default=OUT_CANDIDATES,
        help=f"Output CSV (default: {OUT_CANDIDATES.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--min-per-chan", type=float, default=0.5,
        help="Minimum per-channel correlation to consider strong (default 0.5)",
    )
    parser.add_argument(
        "--min-templates", type=int, default=2,
        help="Minimum distinct templates that must fire on the same event (default 2)",
    )
    parser.add_argument(
        "--single-template-thresh", type=float, default=0.65,
        help="A single-template detection still counts if per-chan >= this "
             "(default 0.65 — strong single matches aren't always wrong)",
    )
    parser.add_argument(
        "--cluster-window", type=float, default=5.0,
        help="Group detections within this many seconds into one event (default 5)",
    )
    parser.add_argument(
        "--exclude-tolerance", type=float, default=15.0,
        help="Drop candidates within this many seconds of a confirmed event (default 15)",
    )
    parser.add_argument(
        "--include-known", action="store_true",
        help="DON'T exclude candidates near confirmed events (useful for validation)",
    )
    args = parser.parse_args()

    if not args.detections.exists():
        print(f"ERROR: detections file not found: {args.detections}", file=sys.stderr)
        print("       Run scripts/run_template_match.py first.", file=sys.stderr)
        return 1

    # ── Load + threshold raw detections ──────────────────────────────
    raw = 0
    strong = []
    with args.detections.open() as f:
        for r in csv.DictReader(f):
            raw += 1
            pc = abs(float(r["correlation"])) / int(r["n_chans"])
            if pc >= args.min_per_chan:
                r["per_chan"] = pc
                strong.append(r)
    strong.sort(key=lambda r: r["detect_time"])

    # ── Cluster simultaneous detections into candidate events ────────
    events = []
    i = 0
    while i < len(strong):
        t0 = UTCDateTime(strong[i]["detect_time"])
        group = [strong[i]]
        j = i + 1
        while (
            j < len(strong)
            and UTCDateTime(strong[j]["detect_time"]) - t0 <= args.cluster_window
        ):
            group.append(strong[j])
            j += 1
        events.append(group)
        i = j

    # ── Apply candidate criteria ─────────────────────────────────────
    candidates = []
    for e in events:
        n_t = len({d["template_id"] for d in e})
        best = max(e, key=lambda r: r["per_chan"])
        if n_t >= args.min_templates or best["per_chan"] >= args.single_template_thresh:
            candidates.append({
                "time": e[0]["detect_time"],
                "n_templates": n_t,
                "best_per_chan": round(best["per_chan"], 3),
                "best_template": best["template_id"],
                "hr_utc": int(e[0]["detect_time"][11:13]),
            })

    # ── Exclude candidates that match confirmed events ───────────────
    excluded = 0
    if not args.include_known:
        confirmed_times = load_confirmed_event_times()
        kept = []
        for c in candidates:
            t = UTCDateTime(c["time"])
            if any(abs(t - ct) <= args.exclude_tolerance for ct in confirmed_times):
                excluded += 1
                continue
            kept.append(c)
        candidates = kept

    candidates.sort(key=lambda c: c["time"])

    # ── Write outputs ────────────────────────────────────────────────
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["time", "n_templates", "best_per_chan", "best_template", "hr_utc"],
        )
        w.writeheader()
        for c in candidates:
            w.writerow(c)

    # Provenance / summary
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {
            "min_per_chan": args.min_per_chan,
            "min_templates": args.min_templates,
            "single_template_thresh": args.single_template_thresh,
            "cluster_window_s": args.cluster_window,
            "exclude_tolerance_s": args.exclude_tolerance,
            "include_known": args.include_known,
        },
        "counts": {
            "raw_detections": raw,
            "strong_per_chan_ge_threshold": len(strong),
            "clustered_events": len(events),
            "excluded_known": excluded,
            "candidates_kept": len(candidates),
        },
    }
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))

    print(f"Raw detections:                       {raw:>10,}")
    print(f"Strong (per-channel ≥ {args.min_per_chan}):           {len(strong):>10,}")
    print(f"Clustered events (within {args.cluster_window:.0f}s):       {len(events):>10,}")
    if not args.include_known:
        print(f"Excluded (within {args.exclude_tolerance:.0f}s of confirmed):   {excluded:>10,}")
    print(f"Candidates kept:                      {len(candidates):>10,}")
    print()
    print(f"Wrote {args.out.relative_to(ROOT)}")
    print(f"Wrote {OUT_SUMMARY.relative_to(ROOT)} (params + counts for reproducibility)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
