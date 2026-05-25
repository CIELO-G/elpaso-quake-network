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

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# scipy >=1.15 moved window functions from scipy.signal to scipy.signal.windows;
# ObsPy 1.4 still references the old location.  Shim them back so that
# st.taper(type="hann") keeps working without pinning scipy.
import scipy.signal
for _wf in ("hann", "hamming", "blackman"):
    if not hasattr(scipy.signal, _wf) and hasattr(scipy.signal.windows, _wf):
        setattr(scipy.signal, _wf, getattr(scipy.signal.windows, _wf))

from obspy import UTCDateTime, read
from obspy import read_inventory

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations
from lib.logger import MetricsWriter, setup_logging
from lib.pipeline_stage import iter_days, resolve_time_window


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
    "pre_filt": [0.5, 1.0, 40.0, 45.0],
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
    # Per-station parallelism. None = auto (min(cpu_count, n_stations)).
    # Set to 1 to force serial processing (e.g. when debugging or under
    # tight memory pressure). Each worker holds a full StationXML + the
    # current trace's float64 buffers, so 4-8 workers ≈ a few GB peak.
    "max_workers": None,
}


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_raw_files(
    station_cfg: dict,
    input_dir: str,
    start_time: UTCDateTime,
    end_time: UTCDateTime,
) -> list[Path]:
    """Walk julian-day directories and return raw miniSEED paths in the time range."""
    raw_dir = Path(input_dir) / "1-raw"
    network = station_cfg["network"]
    station = station_cfg["station"]

    matched: list[Path] = []
    for year_str, jday_str, _ in iter_days(start_time, end_time):
        day_dir = raw_dir / year_str / jday_str
        if day_dir.is_dir():
            pattern = f"{network}.{station}.*.*.{year_str}.{jday_str}.mseed"
            matched.extend(sorted(day_dir.glob(pattern)))
    return matched


def get_output_path(raw_path: Path, input_dir: str, output_dir: str) -> Path:
    """Map a raw file path to its processed counterpart."""
    raw_path = Path(raw_path)
    input_raw = Path(input_dir) / "1-raw"
    rel = raw_path.relative_to(input_raw)
    return Path(output_dir) / "2-processed" / rel


# ---------------------------------------------------------------------------
# Core ObsPy processing pipeline
# ---------------------------------------------------------------------------

def process_file(
    raw_path: Path,
    inventory,
    output_path: Path,
    config: dict,
    logger,
) -> bool:
    """Read a raw miniSEED file, apply instrument-response removal and
    bandpass filtering, then write the processed result.

    Returns True if processing succeeded, False otherwise.
    """
    try:
        st = read(str(raw_path))
    except Exception as exc:
        logger.error("Cannot read %s: %s: %s", raw_path, type(exc).__name__, exc)
        return False

    # Guard against empty or single-sample streams
    if len(st) == 0:
        logger.warning("Empty stream from %s, skipping", raw_path)
        return False

    try:
        # 1. Merge overlapping/adjacent traces from chunked ingestion
        st.merge(method=1, fill_value="interpolate")

        # Guard: after merge, check for traces with too few samples
        st_filtered = st.select()
        for tr in list(st_filtered):
            if tr.stats.npts < 10:
                logger.warning(
                    "Removing trace %s with only %d samples from %s",
                    tr.id, tr.stats.npts, raw_path.name,
                )
                st_filtered.remove(tr)
        if len(st_filtered) == 0:
            logger.warning("No usable traces after merge in %s", raw_path)
            return False
        st = st_filtered

        # 2. Detrend
        for method in config["detrend_methods"]:
            st.detrend(method)

        # 3. Taper
        st.taper(max_percentage=config["taper_fraction"], type="hann")

        # 4. Trim traces that start before their metadata validity window.
        #    Raspberry Shake stations can record samples before the StationXML
        #    epoch, causing "No matching response information found" from ObsPy.
        #    This also handles first-day-online cases where the station came
        #    online mid-day but raw data starts at 00:00.
        _MAX_TRIM_SEC = 86400.0  # allow trimming up to 24 h (first-day-online)
        for tr in list(st):
            # Check if inventory already covers the trace start
            sel_ok = inventory.select(
                network=tr.stats.network, station=tr.stats.station,
                location=tr.stats.location, channel=tr.stats.channel,
                time=tr.stats.starttime,
            )
            if sel_ok and sel_ok[0] and sel_ok[0][0] and sel_ok[0][0][0]:
                continue  # metadata covers trace start — no trim needed

            # Find the nearest channel epoch that starts just after the trace
            sel_all = inventory.select(
                network=tr.stats.network, station=tr.stats.station,
                location=tr.stats.location, channel=tr.stats.channel,
            )
            best_start = None
            for net in sel_all:
                for sta in net:
                    for chan in sta:
                        cs = UTCDateTime(chan.start_date)
                        gap = cs - tr.stats.starttime
                        if 0 < gap <= _MAX_TRIM_SEC:
                            if best_start is None or cs < best_start:
                                best_start = cs

            if best_start is not None:
                gap = best_start - tr.stats.starttime
                logger.info(
                    "Trimming %s start by %.0f s to match metadata epoch",
                    tr.id, gap,
                )
                tr.trim(starttime=best_start)
                if tr.stats.npts < 10:
                    logger.warning(
                        "Trace %s too short after metadata trim, removing",
                        tr.id,
                    )
                    st.remove(tr)

        if len(st) == 0:
            logger.warning("No usable traces after metadata trim in %s", raw_path)
            return False

        # 5. Remove instrument response
        pre_filt = config["pre_filt"]
        if isinstance(pre_filt, list):
            pre_filt = tuple(pre_filt)

        st.remove_response(
            inventory=inventory,
            output=config["response_output"],
            pre_filt=pre_filt,
            water_level=config["water_level"],
        )

        # 6. Bandpass filter removed — the pre_filt cosine taper in
        # remove_response() already band-limits the signal. A separate
        # bandpass caused redundant double-filtering at band edges,
        # distorting amplitudes. See SCIENCE_AUDIT.md Issue 2.

        # 7. Write output (float32 keeps ~7 sig figs; halves size vs float64)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        for tr in st:
            tr.data = tr.data.astype("float32")
        st.write(str(output_path), format="MSEED", encoding="FLOAT32")

        npts = sum(tr.stats.npts for tr in st)
        logger.info("Processed %s -> %s (%d samples)", raw_path.name, output_path, npts)
        return True

    except ValueError as exc:
        logger.error(
            "Processing value error for %s: %s: %s",
            raw_path, type(exc).__name__, exc,
        )
        return False
    except Exception as exc:
        logger.error(
            "Processing failed for %s: %s: %s",
            raw_path, type(exc).__name__, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Per-station processing
# ---------------------------------------------------------------------------

def process_station(
    station_cfg: dict,
    config: dict,
    logger,
    start_time: UTCDateTime,
    end_time: UTCDateTime,
    force: bool,
) -> dict[str, int]:
    """Load metadata once per station, then iterate over raw files."""
    network = station_cfg["network"]
    station = station_cfg["station"]

    logger.info(
        "--- %s.%s | %s -> %s ---",
        network, station, start_time, end_time,
    )

    counts: dict[str, int] = {"processed": 0, "skipped": 0, "failed": 0}

    # Load StationXML metadata
    metadata_dir = Path(config["input_dir"]) / "1-metadata"
    xml_path = metadata_dir / f"{network}.{station}.xml"

    if not xml_path.exists():
        logger.error(
            "No metadata file for %s.%s at %s -- skipping station",
            network, station, xml_path,
        )
        return counts

    try:
        inventory = read_inventory(str(xml_path))
    except Exception as exc:
        logger.error(
            "Cannot read metadata for %s.%s: %s: %s -- skipping station",
            network, station, type(exc).__name__, exc,
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

    # Explicit cleanup after each station
    gc.collect()

    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
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


def main() -> None:
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("process", config, debug=args.debug)
    metrics = MetricsWriter(Path(config["log_dir"]) / "metrics.jsonl")

    logger.info("=" * 60)
    logger.info("Waveform preprocessing -- %d station(s)", len(stations))
    logger.info("=" * 60)

    # Determine time window
    start_time, end_time = resolve_time_window(
        args, config,
        window_hours_key="process_window_hours",
        latency_hours_key="process_latency_hours",
        logger=logger,
    )

    force = args.force
    if force:
        logger.info("Force mode enabled -- reprocessing all files")

    # Worker count: auto-pick min(cpu, n_stations) unless overridden in config.
    # ObsPy's response removal + filters are dominated by FFT/numpy work that
    # releases the GIL, so a ThreadPoolExecutor scales nearly like processes
    # for this workload without the pickling cost.
    n_stations = len(stations)
    configured = config.get("max_workers")
    if configured is None:
        max_workers = min(os.cpu_count() or 1, n_stations)
    else:
        max_workers = max(1, min(int(configured), n_stations))
    logger.info("Parallelism: %d worker(s) for %d station(s)", max_workers, n_stations)

    totals: dict[str, int] = {"processed": 0, "skipped": 0, "failed": 0}

    def _run_station(station_cfg: dict) -> tuple[dict, str, float]:
        """Worker payload: process one station, return (counts, sta_id, elapsed)."""
        sta_id = f"{station_cfg['network']}.{station_cfg['station']}"
        t0 = time.monotonic()
        try:
            counts = process_station(
                station_cfg, config, logger, start_time, end_time, force,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error processing %s: %s: %s",
                sta_id, type(exc).__name__, exc,
            )
            counts = {"processed": 0, "skipped": 0, "failed": 0}
        return counts, sta_id, time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="proc") as ex:
        futures = [ex.submit(_run_station, s) for s in stations]
        for fut in as_completed(futures):
            counts, sta_id, elapsed = fut.result()
            for k in totals:
                totals[k] += counts[k]
            metrics.record("process", station=sta_id, duration_s=round(elapsed, 1))

    # Summary
    logger.info("=" * 60)
    logger.info(
        "Done. processed=%d  skipped=%d  failed=%d",
        totals["processed"], totals["skipped"], totals["failed"],
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
