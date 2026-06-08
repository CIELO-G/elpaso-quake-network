#!/usr/bin/env python3
"""Run EQcorrscan matched-filter detection against the processed daily data.

Loads the template library built by ``build_templates.py``, scans the
processed waveforms for each day in the requested range, and writes a
CSV of detections (one row per matched event).

Usage
-----
    python scripts/run_template_match.py --start 2026-04-01 --end 2026-04-15
    python scripts/run_template_match.py --start 2026-05-01 --threshold 9
    python scripts/run_template_match.py --start 2026-05-01 --threshold-type absolute --threshold 0.6

Outputs
-------
    output/templates/detections.csv   (appended; one row per detection)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

from obspy import Stream, UTCDateTime, read

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROCESSED_DIR = ROOT / "output" / "2-processed"
TEMPLATES_PATH = ROOT / "output" / "templates" / "blast_templates.tgz"
DETECTIONS_PATH = ROOT / "output" / "templates" / "detections.csv"

# Hide chatty ObsPy / EQcorrscan warnings (mseed encoding, deprecation)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


def iter_days(start: date, end: date):
    """Yield (year, jday, datetime) for each calendar day in [start, end]."""
    d = start
    while d <= end:
        yield d.year, d.timetuple().tm_yday, d
        d += timedelta(days=1)


def load_day_stream(year: int, jday: int) -> Stream:
    """Read every processed mseed file for one day into a single Stream."""
    day_dir = PROCESSED_DIR / f"{year:04d}" / f"{jday:03d}"
    if not day_dir.exists():
        return Stream()
    stream = Stream()
    for mseed in sorted(day_dir.glob("*.mseed")):
        try:
            stream += read(str(mseed))
        except Exception:
            continue
    return stream


def write_detection_rows(rows: list[dict]) -> None:
    """Append detection rows to the CSV (create with header if absent)."""
    if not rows:
        return
    DETECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "detect_time", "template_id", "correlation", "threshold", "n_chans", "day",
    ]
    write_header = not DETECTIONS_PATH.exists()
    with DETECTIONS_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for row in rows:
            w.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD (default = start)")
    parser.add_argument("--threshold", type=float, default=8.0,
                        help="Detection threshold (default 8 for MAD type)")
    parser.add_argument("--threshold-type", default="MAD",
                        choices=["MAD", "absolute", "av_chan_corr"],
                        help="Threshold type (default MAD)")
    parser.add_argument("--trig-int", type=float, default=6.0,
                        help="Min seconds between two detections of the same template")
    parser.add_argument("--templates", type=Path, default=TEMPLATES_PATH,
                        help="Template tribe archive")
    parser.add_argument("--cores", type=int, default=4,
                        help="Parallel cores for matched-filter (default 4)")
    parser.add_argument("--fresh", action="store_true",
                        help="Wipe existing detections.csv before running")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # EQcorrscan is very chatty at INFO — kick it down to WARNING
    logging.getLogger("eqcorrscan").setLevel(logging.WARNING)
    logging.getLogger("obspy").setLevel(logging.WARNING)
    logger = logging.getLogger("template_match")

    from eqcorrscan.core.match_filter import Tribe

    if not args.templates.exists():
        logger.error("Template archive not found at %s", args.templates)
        logger.error("Run scripts/build_templates.py first.")
        sys.exit(1)

    if args.fresh and DETECTIONS_PATH.exists():
        logger.info("--fresh: removing existing %s", DETECTIONS_PATH)
        DETECTIONS_PATH.unlink()

    logger.info("Loading templates from %s …", args.templates)
    tribe = Tribe().read(str(args.templates))
    logger.info("Loaded %d templates", len(tribe))

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else start

    total_dets = 0
    days_processed = 0
    days_skipped = 0
    for year, jday, d in iter_days(start, end):
        day_iso = d.isoformat()
        stream = load_day_stream(year, jday)
        if not len(stream):
            logger.info("%s — no processed data, skip", day_iso)
            days_skipped += 1
            continue
        # EQcorrscan expects merged continuous traces
        stream.merge(fill_value=0)
        # match templates' filter+rate
        try:
            party = tribe.detect(
                stream=stream,
                threshold=args.threshold,
                threshold_type=args.threshold_type,
                trig_int=args.trig_int,
                plot=False,
                cores=args.cores,
                ignore_length=True,
                ignore_bad_data=True,
                parallel_process=False,
            )
        except Exception as e:
            logger.warning("%s — match_filter failed: %s", day_iso, e)
            days_skipped += 1
            continue
        # Party is a list of Family (one per template); each Family contains
        # its own Detection list. Flatten.
        rows = []
        for family in party:
            for det in family:
                rows.append({
                    "detect_time": str(det.detect_time),
                    "template_id": det.template_name,
                    "correlation": round(float(det.detect_val), 4),
                    "threshold": round(float(det.threshold), 4),
                    "n_chans": det.no_chans,
                    "day": day_iso,
                })
        write_detection_rows(rows)
        total_dets += len(rows)
        days_processed += 1
        logger.info("%s — %d detections", day_iso, len(rows))

    logger.info("=" * 50)
    logger.info("Done. days=%d  skipped=%d  total_detections=%d",
                days_processed, days_skipped, total_dets)
    logger.info("Output: %s", DETECTIONS_PATH)


if __name__ == "__main__":
    main()
