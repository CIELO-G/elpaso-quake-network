#!/usr/bin/env bash
# backup_to_drive.sh — Archive pipeline data to external drive
#
# Long-term archive of the El Paso seismic network data.
# Uses rsync for incremental copies. Safe to re-run; only new/changed
# files are transferred.
#
# Usage:
#   ./backup_to_drive.sh              # normal sync
#   ./backup_to_drive.sh --dry-run    # preview what would be copied

set -euo pipefail

# Repo root = this script's directory (no hard-coded home path; survives
# moving the repo or changing user). Archive drive overridable via env.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE_DRIVE="${ELPASO_ARCHIVE_DRIVE:-/Volumes/Marc}"
DST="$ARCHIVE_DRIVE/Data/Research/Elpaso/elpaso-quake-network"

# Check drive is mounted
if [ ! -d "$ARCHIVE_DRIVE" ]; then
    echo "ERROR: $ARCHIVE_DRIVE is not mounted. Plug in the drive and retry."
    exit 1
fi

# Pass through flags (e.g. --dry-run)
EXTRA_FLAGS="${*:-}"

mkdir -p "$DST"

echo "=== Archiving elpaso-quake-network to $DST ==="
echo ""

# 1. Output data (the big one: ~545 GB as of Aug 2026, and growing)
# NOTE: local 1-raw is pruned after verified backup (scripts/prune_raw.py);
# this archive keeps the only local copy of old raw days. NEVER add
# --delete to this rsync — it would erase pruned days from the archive.
echo "--- output/ (raw ~160G + processed ~385G + picks + events + catalog + nlloc + metadata + caches) ---"
rsync -avh --progress $EXTRA_FLAGS "$SRC/output/" "$DST/output/"

# 2. Station definitions and response metadata
echo ""
echo "--- stations.json ---"
rsync -avh --progress $EXTRA_FLAGS "$SRC/stations.json" "$DST/"
echo ""
echo "--- stations/ (StationXML response files) ---"
rsync -avh --progress $EXTRA_FLAGS "$SRC/stations/" "$DST/stations/"

# 3. Config files (all step configs)
echo ""
echo "--- config files ---"
for step_dir in 1-ingestion 2-processing 3-detection 4-association; do
    cfg="$SRC/$step_dir/config.yaml"
    if [ -f "$cfg" ]; then
        mkdir -p "$DST/$step_dir"
        rsync -avh --progress $EXTRA_FLAGS "$cfg" "$DST/$step_dir/"
    fi
done

# 4. Logs and metrics
echo ""
echo "--- logs/ ---"
rsync -avh --progress $EXTRA_FLAGS "$SRC/logs/" "$DST/logs/"

# 5. Download tracker DB (for resumable ingestion)
echo ""
echo "--- download tracker DB ---"
for f in "$SRC/output/1-downloads.db"*; do
    [ -f "$f" ] && rsync -avh --progress $EXTRA_FLAGS "$f" "$DST/output/"
done

# 6. Log this backup
if [[ ! "$EXTRA_FLAGS" == *"--dry-run"* ]]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') — backup completed" >> "$DST/output/backup.log"
fi

echo ""
echo "=== Archive complete ==="
