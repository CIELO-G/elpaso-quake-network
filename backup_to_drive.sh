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

SRC="/Users/magarcia113/Research/Elpaso/elpaso-quake-network"
DST="/Volumes/Marc/Data/Research/Elpaso/elpaso-quake-network"

# Check drive is mounted
if [ ! -d "/Volumes/Marc" ]; then
    echo "ERROR: /Volumes/Marc is not mounted. Plug in the drive and retry."
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
# On the Mac Studio, output/1-raw and output/2-processed are symlinks to the
# RAID. rsync -a would copy the links themselves, so sync those two trees
# explicitly (trailing slash follows the link) and exclude them here. The
# guard stops an unmounted RAID from looking like an empty source.
for d in 1-raw 2-processed; do
    if [ ! -d "$SRC/output/$d/" ]; then
        echo "ERROR: $SRC/output/$d is not reachable (RAID unmounted?). Aborting."
        exit 1
    fi
done
rsync -avh --progress $EXTRA_FLAGS \
    --exclude '/1-raw' --exclude '/2-processed' \
    "$SRC/output/" "$DST/output/"
for d in 1-raw 2-processed; do
    mkdir -p "$DST/output/$d"
    rsync -avh --progress $EXTRA_FLAGS "$SRC/output/$d/" "$DST/output/$d/"
done

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
