"""Shared constants for the El Paso seismic pipeline.

Centralises hardcoded values that were previously scattered across modules.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Output directory structure (relative names within output_dir)
RAW_SUBDIR = "1-raw"
METADATA_SUBDIR = "1-metadata"
PROCESSED_SUBDIR = "2-processed"
PICKS_SUBDIR = "3-picks"
EVENTS_SUBDIR = "4-events"
CATALOG_SUBDIR = "5-catalog"

# ---------------------------------------------------------------------------
# Network geometry (approximate centroid of El Paso stations)
# ---------------------------------------------------------------------------

NETWORK_CENTER_LAT = 31.85
NETWORK_CENTER_LON = -106.40

# Half-width (in degrees) of the study area, centred on the network. THE
# single definition of the box: GaMMA's search region (4-association),
# the NLLoc travel-time grid extent (lib/location/nlloc_config.py), the
# dashboard's study-area rectangle (/api/study_area) and the station
# validation (a station must lie inside) all derive from it.
# ±1.0° ≈ ±111 km N-S, ±94 km E-W at this latitude: network footprint +
# Vado/Anthony quarry belt + Las Cruces. Changing it means rebuilding the
# NLLoc grids (scripts/nlloc_build_grids.py).
NETWORK_HALF_WIDTH_DEG = 1.0

# Average degrees-to-km conversion at mid-latitude. Used to translate the
# degree-based search region into km for GaMMA's projected frame.
DEGREES_TO_KM = 111.19

# ---------------------------------------------------------------------------
# Pipeline operational defaults
# ---------------------------------------------------------------------------

# Buffer after a UTC day ends before that day is fetched and processed.
# Measured 2026-10-12: Raspberry Shake FDSN serves data ~30 min behind real
# time, IRIS (EP.KIDD) in real time; and since Phase 1 any short hour is
# re-requested for 7 days, so late uploads are caught regardless. 2 h keeps
# margin for a slow evening at the server. (Was 6 h: day ran at 06:00 UTC.)
DATA_LATENCY_HOURS = 2

# Total lag for "is today's data eligible to process?" in continuous mode:
# 24 h for the day to complete + DATA_LATENCY_HOURS FDSNWS finalisation buffer.
CONTINUOUS_LAG_HOURS = 24 + DATA_LATENCY_HOURS

DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_WAIT_SECONDS = 120
PIPELINE_START_DATE_ISO = "2025-11-01"

# Inter-station polite delay (seconds)
DEFAULT_STATION_DELAY_SECONDS = 2

# ---------------------------------------------------------------------------
# FDSNWS circuit breaker
# ---------------------------------------------------------------------------

CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive 503 errors before backing off
CIRCUIT_BREAKER_BACKOFF_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# File naming conventions
# ---------------------------------------------------------------------------

MSEED_EXTENSION = ".mseed"
PICKS_CSV_SUFFIX = ".picks.csv"
EVENTS_CSV_SUFFIX = ".events.csv"
ASSIGNMENTS_CSV_SUFFIX = ".assignments.csv"


def raw_file_name(net: str, sta: str, loc: str, cha: str, year: str, jday: str) -> str:
    """Construct a raw miniSEED filename."""
    return f"{net}.{sta}.{loc}.{cha}.{year}.{jday}{MSEED_EXTENSION}"


def daily_picks_path(output_dir: str, year: str, jday: str) -> Path:
    """Return the CSV path for a day's picks."""
    return Path(output_dir) / PICKS_SUBDIR / year / jday / f"{year}.{jday}{PICKS_CSV_SUFFIX}"


def daily_events_paths(output_dir: str, year: str, jday: str) -> tuple[Path, Path]:
    """Return (events_csv, assignments_csv) for a day's association output."""
    base = Path(output_dir) / EVENTS_SUBDIR / year / jday
    return (
        base / f"{year}.{jday}{EVENTS_CSV_SUFFIX}",
        base / f"{year}.{jday}{ASSIGNMENTS_CSV_SUFFIX}",
    )
