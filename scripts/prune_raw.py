"""Prune local raw day-directories that are safely archived on the drive.

Deletes ``output/1-raw/YYYY/JJJ`` day-dirs from the Mac ONLY when the same
day in the archive contains every local file with an identical byte size
(rsync-verified copy). The newest ``--keep-days`` days are always kept for
the in-flight pipeline. Processed data is never touched.

    python scripts/prune_raw.py              # dry run: report only
    python scripts/prune_raw.py --apply      # actually delete
    python scripts/prune_raw.py --keep-days 10

Restoring a pruned day (needed to re-run detection/processing for it):

    rsync -avh <archive>/elpaso-quake-network/output/1-raw/YYYY/JJJ/ output/1-raw/YYYY/JJJ/
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCAL_RAW = REPO / "output" / "1-raw"
ARCHIVE_RAW = Path("/Volumes/Marc/Data/Research/Elpaso/elpaso-quake-network/output/1-raw")


def day_dirs(base: Path):
    """Yield (year, jday, path) for every YYYY/JJJ dir under base."""
    for ydir in sorted(base.glob("[12][0-9][0-9][0-9]")):
        for ddir in sorted(ydir.glob("[0-3][0-9][0-9]")):
            yield ydir.name, ddir.name, ddir


def verify_archived(local_day: Path, archive_day: Path) -> tuple[bool, str]:
    """Every local file must exist in the archive with the same size."""
    if not archive_day.is_dir():
        return False, "day missing from archive"
    for f in local_day.iterdir():
        if not f.is_file():
            continue
        a = archive_day / f.name
        if not a.is_file():
            return False, f"missing in archive: {f.name}"
        if a.stat().st_size != f.stat().st_size:
            return False, f"size mismatch: {f.name}"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="delete verified days (default: dry-run report)")
    ap.add_argument("--keep-days", type=int, default=5,
                    help="always keep the newest N calendar days (default 5)")
    args = ap.parse_args()

    if not ARCHIVE_RAW.is_dir():
        print(f"ERROR: archive not mounted ({ARCHIVE_RAW})")
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)
    to_delete, skipped, kept_recent = [], [], 0
    for year, jday, ddir in day_dirs(LOCAL_RAW):
        day_date = datetime.strptime(f"{year} {jday}", "%Y %j").replace(
            tzinfo=timezone.utc)
        if day_date >= cutoff:
            kept_recent += 1
            continue
        ok, why = verify_archived(ddir, ARCHIVE_RAW / year / jday)
        size = sum(f.stat().st_size for f in ddir.iterdir() if f.is_file())
        if ok:
            to_delete.append((ddir, size))
        else:
            skipped.append((ddir, why))

    total_gb = sum(s for _, s in to_delete) / 1e9
    print(f"{len(to_delete)} day-dirs verified in archive "
          f"({total_gb:.1f} GB), {kept_recent} recent days kept, "
          f"{len(skipped)} NOT archived (kept)")
    for d, why in skipped:
        print(f"  KEEP {d.relative_to(LOCAL_RAW)}: {why}")

    if not args.apply:
        print("\nDry run — nothing deleted. Re-run with --apply to delete.")
        return 0

    for d, _ in to_delete:
        shutil.rmtree(d)
    # Remove now-empty year dirs
    for ydir in LOCAL_RAW.glob("[12][0-9][0-9][0-9]"):
        if ydir.is_dir() and not any(ydir.iterdir()):
            ydir.rmdir()
    print(f"Deleted {len(to_delete)} day-dirs ({total_gb:.1f} GB freed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
