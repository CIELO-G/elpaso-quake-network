#!/usr/bin/env python3
"""
process.py - Seismic waveform preprocessing

Step 2/5 of the El Paso seismic processing pipeline.
Removes instrument response, applies bandpass filtering, and writes
corrected waveforms for downstream detection.

Usage (run from project root):
    python 2-processing/process.py --config 2-processing/config.yaml
    python 2-processing/process.py --config 2-processing/config.yaml --start 2026-01-29 --end 2026-01-29
    python 2-processing/process.py --config 2-processing/config.yaml --force --debug
"""

import argparse
import sys
from pathlib import Path

from obspy import UTCDateTime, read
from obspy import read_inventory

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations
from lib.logger import setup_logging


# ---------------------------------------------------------------------------
# Configuration defaults (processing-specific)
# ---------------------------------------------------------------------------

DEFAULTS = {
    "stations_file": "stations.json",
    "input_dir": "output",
    "output_dir": "output",
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
    "filter_freqmin": 1.0,
    "filter_freqmax": 45.0,
    "pre_filt": [0.5, 1.0, 45.0, 49.0],
    "response_output": "VEL",
    "water_level": 60,
    "taper_fraction": 0.05,
    "detrend_methods": ["demean", "linear"],
    "filter_order": 4,
    "filter_zerophase": True,
    "process_window_hours": 24,
    "process_latency_hours": 6,
    "default_channels": "EHZ",
    "default_location": "00",
}


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_raw_files(station_cfg, input_dir, start_time, end_time):
    """Walk julian-day directories and return raw miniSEED paths in the time range.

    Parameters
    ----------
    station_cfg : dict
        Station entry from stations.json.
    input_dir : str
        Base data directory (contains ``raw/``).
    start_time, end_time : UTCDateTime
        Inclusive time range to search.

    Returns
    -------
    list of Path
        Sorted list of matching miniSEED file paths.
    """
    raw_dir = Path(input_dir) / "1-raw"
    network = station_cfg["network"]
    station = station_cfg["station"]

    matched = []

    day = UTCDateTime(start_time.year, start_time.month, start_time.day)
    end_day = UTCDateTime(end_time.year, end_time.month, end_time.day)

    while day <= end_day:
        year_str = str(day.year)
        jday_str = f"{day.julday:03d}"
        day_dir = raw_dir / year_str / jday_str

        if day_dir.is_dir():
            pattern = f"{network}.{station}.*.*.{year_str}.{jday_str}.mseed"
            for f in sorted(day_dir.glob(pattern)):
                matched.append(f)

        day += 86400

    return matched


def get_output_path(raw_path, input_dir, output_dir):
    """Map a raw file path to its processed counterpart.

    ``output/1-raw/2026/029/AM.R0F2D.00.EHZ.2026.029.mseed``
    becomes
    ``output/2-processed/2026/029/AM.R0F2D.00.EHZ.2026.029.mseed``
    """
    raw_path = Path(raw_path)
    input_raw = Path(input_dir) / "1-raw"
    rel = raw_path.relative_to(input_raw)
    return Path(output_dir) / "2-processed" / rel


# ---------------------------------------------------------------------------
# Core ObsPy processing pipeline
# ---------------------------------------------------------------------------

def process_file(raw_path, inventory, output_path, config, logger):
    """Read a raw miniSEED file, apply instrument-response removal and
    bandpass filtering, then write the processed result.

    Parameters
    ----------
    raw_path : Path
        Input miniSEED file.
    inventory : Inventory
        StationXML inventory with response information.
    output_path : Path
        Destination miniSEED file.
    config : dict
        Processing parameters.
    logger : logging.Logger

    Returns
    -------
    bool
        True if processing succeeded, False otherwise.
    """
    try:
        st = read(str(raw_path))
    except Exception as exc:
        logger.error("Cannot read %s: %s", raw_path, exc)
        return False

    try:
        # 1. Merge overlapping/adjacent traces from chunked ingestion
        st.merge(method=1, fill_value=0)

        # 2. Detrend
        for method in config["detrend_methods"]:
            st.detrend(method)

        # 3. Taper
        st.taper(max_percentage=config["taper_fraction"], type="hann")

        # 4. Remove instrument response
        pre_filt = config["pre_filt"]
        if isinstance(pre_filt, list):
            pre_filt = tuple(pre_filt)

        st.remove_response(
            inventory=inventory,
            output=config["response_output"],
            pre_filt=pre_filt,
            water_level=config["water_level"],
        )

        # 5. Bandpass filter
        st.filter(
            "bandpass",
            freqmin=config["filter_freqmin"],
            freqmax=config["filter_freqmax"],
            corners=config["filter_order"],
            zerophase=config["filter_zerophase"],
        )

        # 6. Write output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        st.write(str(output_path), format="MSEED")

        npts = sum(tr.stats.npts for tr in st)
        logger.info("Processed %s -> %s (%d samples)", raw_path.name, output_path, npts)
        return True

    except Exception as exc:
        logger.error("Processing failed for %s: %s", raw_path, exc)
        return False


# ---------------------------------------------------------------------------
# Per-station processing
# ---------------------------------------------------------------------------

def process_station(station_cfg, config, logger, start_time, end_time, force):
    """Load metadata once per station, then iterate over raw files.

    Parameters
    ----------
    station_cfg : dict
        Station entry from stations.json.
    config : dict
        Full configuration.
    logger : logging.Logger
    start_time, end_time : UTCDateTime
        Time range to process.
    force : bool
        If True, reprocess files even if output already exists.

    Returns
    -------
    dict
        Counters: ``{"processed": int, "skipped": int, "failed": int}``.
    """
    network = station_cfg["network"]
    station = station_cfg["station"]

    logger.info(
        "--- %s.%s | %s -> %s ---",
        network, station, start_time, end_time,
    )

    counts = {"processed": 0, "skipped": 0, "failed": 0}

    # Load StationXML metadata
    metadata_dir = Path(config["input_dir"]) / "1-metadata"
    xml_path = metadata_dir / f"{network}.{station}.xml"

    if not xml_path.exists():
        logger.error(
            "No metadata file for %s.%s at %s — skipping station",
            network, station, xml_path,
        )
        return counts

    try:
        inventory = read_inventory(str(xml_path))
    except Exception as exc:
        logger.error(
            "Cannot read metadata for %s.%s: %s — skipping station",
            network, station, exc,
        )
        return counts

    # Find raw files in the time range
    raw_files = find_raw_files(station_cfg, config["input_dir"], start_time, end_time)

    if not raw_files:
        logger.info("No raw files found for %s.%s in time range", network, station)
        return counts

    logger.info("Found %d raw file(s) for %s.%s", len(raw_files), network, station)

    for raw_path in raw_files:
        output_path = get_output_path(raw_path, config["input_dir"], config["output_dir"])

        # Skip if already processed (unless --force)
        if output_path.exists() and not force:
            logger.debug("Already processed, skipping: %s", output_path)
            counts["skipped"] += 1
            continue

        ok = process_file(raw_path, inventory, output_path, config, logger)
        if ok:
            counts["processed"] += 1
        else:
            counts["failed"] += 1

    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Preprocess seismic waveforms (instrument response removal, filtering)",
    )
    parser.add_argument("--config", required=True,
                        help="Path to YAML configuration file")
    parser.add_argument("--start",
                        help="Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end",
                        help="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess files even if output already exists")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging")
    return parser.parse_args()


def main():
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("process", config, debug=args.debug)

    logger.info("=" * 60)
    logger.info("Waveform preprocessing — %d station(s)", len(stations))
    logger.info("=" * 60)

    # Determine time window
    if args.start and args.end:
        start_time = UTCDateTime(args.start)
        end_time = UTCDateTime(args.end)
        logger.info("Explicit range: %s -> %s", start_time, end_time)
    elif args.start or args.end:
        logger.error("Both --start and --end are required together")
        sys.exit(1)
    else:
        end_time = UTCDateTime() - config["process_latency_hours"] * 3600
        start_time = end_time - config["process_window_hours"] * 3600
        logger.info("Scheduled mode: %s -> %s", start_time, end_time)

    force = args.force
    if force:
        logger.info("Force mode enabled — reprocessing all files")

    # Process each station
    totals = {"processed": 0, "skipped": 0, "failed": 0}

    for station_cfg in stations:
        try:
            counts = process_station(
                station_cfg, config, logger, start_time, end_time, force,
            )
            for k in totals:
                totals[k] += counts[k]
        except Exception as exc:
            logger.error(
                "Unexpected error processing %s.%s: %s",
                station_cfg["network"], station_cfg["station"], exc,
            )

    # Summary
    logger.info("=" * 60)
    logger.info(
        "Done. processed=%d  skipped=%d  failed=%d",
        totals["processed"], totals["skipped"], totals["failed"],
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
