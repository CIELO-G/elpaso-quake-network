#!/usr/bin/env python3
"""
backup.py -- Backup catalog.csv and the downloads SQLite database.

Creates timestamped copies in a backup directory.  Designed to be run
via cron or the Makefile (``make backup``).

Configuration:
    BACKUP_DIR environment variable (default: output/backups)

Usage:
    python scripts/backup.py
    python scripts/backup.py --dest /mnt/nas/seismic-backups
    BACKUP_DIR=/tmp/backups python scripts/backup.py
"""

import argparse
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"

# Files to back up
BACKUP_TARGETS = [
    OUTPUT_DIR / "5-catalog" / "catalog.csv",
    OUTPUT_DIR / "5-catalog" / "assignments.csv",
    OUTPUT_DIR / "1-downloads.db",
]


def parse_args() -> argparse.Namespace:
    default_dest = os.environ.get("BACKUP_DIR", str(OUTPUT_DIR / "backups"))
    p = argparse.ArgumentParser(description="Backup catalog and database files.")
    p.add_argument(
        "--dest",
        type=str,
        default=default_dest,
        help=f"Backup destination directory (default: {default_dest})",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(args.dest) / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"Backup destination: {backup_dir}")

    backed_up = 0
    for src in BACKUP_TARGETS:
        if not src.exists():
            print(f"  Skip (not found): {src.name}")
            continue

        dest = backup_dir / src.name
        shutil.copy2(src, dest)
        size_mb = dest.stat().st_size / 1e6
        print(f"  Copied: {src.name} ({size_mb:.1f} MB)")
        backed_up += 1

    if backed_up == 0:
        print("No files to back up.")
        # Clean up empty directory
        backup_dir.rmdir()
    else:
        print(f"\nBackup complete: {backed_up} files -> {backup_dir}")


if __name__ == "__main__":
    main()
