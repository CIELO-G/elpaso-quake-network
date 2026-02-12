#!/usr/bin/env python3
"""
data_retention.py -- Auto-delete raw waveforms older than N days.

Removes miniSEED files from output/1-raw/ that are older than the
configured retention period.  Empty year/doy directories are cleaned up.

Configuration (in order of precedence):
    1. --days CLI argument
    2. RETENTION_DAYS environment variable
    3. Default: 90 days

Storage growth estimate:
    ~150 MB/day for 11 stations at 100 Hz (3-component), or ~4.5 GB/month.
    At 90-day retention, expect ~13.5 GB maximum in 1-raw/.

Usage:
    python scripts/data_retention.py                  # 90-day default
    python scripts/data_retention.py --days 60        # custom retention
    python scripts/data_retention.py --dry-run        # preview deletions
    RETENTION_DAYS=30 python scripts/data_retention.py
"""

import argparse
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "output" / "1-raw"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Delete raw waveforms older than N days.")
    p.add_argument(
        "--days",
        type=int,
        default=int(os.environ.get("RETENTION_DAYS", "90")),
        help="Retention period in days (default: 90, or RETENTION_DAYS env var)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without actually deleting",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cutoff = date.today() - timedelta(days=args.days)
    print(f"Retention: {args.days} days | Cutoff: {cutoff} | Dry run: {args.dry_run}")

    if not RAW_DIR.exists():
        print(f"Raw directory not found: {RAW_DIR}")
        return

    deleted_dirs = 0
    freed_bytes = 0

    for year_dir in sorted(RAW_DIR.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)

        for doy_dir in sorted(year_dir.iterdir()):
            if not doy_dir.is_dir() or not doy_dir.name.isdigit():
                continue
            doy = int(doy_dir.name)

            try:
                dir_date = date(year, 1, 1) + timedelta(days=doy - 1)
            except ValueError:
                continue

            if dir_date >= cutoff:
                continue

            # Calculate size before deletion
            dir_size = sum(
                f.stat().st_size for f in doy_dir.rglob("*") if f.is_file()
            )

            if args.dry_run:
                print(f"  Would delete: {doy_dir} ({dir_date}, {dir_size / 1e6:.1f} MB)")
            else:
                shutil.rmtree(doy_dir)
                print(f"  Deleted: {doy_dir} ({dir_date}, {dir_size / 1e6:.1f} MB)")

            deleted_dirs += 1
            freed_bytes += dir_size

        # Remove empty year directories
        if year_dir.exists() and not any(year_dir.iterdir()):
            if args.dry_run:
                print(f"  Would remove empty year dir: {year_dir}")
            else:
                year_dir.rmdir()
                print(f"  Removed empty year dir: {year_dir}")

    print(f"\n{'Would delete' if args.dry_run else 'Deleted'}: "
          f"{deleted_dirs} day directories, {freed_bytes / 1e9:.2f} GB freed")


if __name__ == "__main__":
    main()
