#!/usr/bin/env python3
"""
ingest.py - Seismic data ingestion from Raspberry Shake FDSNWS

Step 1/5 of the El Paso seismic processing pipeline.
Fetches waveform data and station metadata, stores as miniSEED files,
and tracks downloads in a local SQLite database.

Usage (run from project root):
    python 1-ingestion/ingest.py --config 1-ingestion/config.yaml
    python 1-ingestion/ingest.py --config 1-ingestion/config.yaml --start 2024-01-01 --end 2024-01-07
    python 1-ingestion/ingest.py --config 1-ingestion/config.yaml --debug
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from obspy import UTCDateTime, Stream, read
from obspy.clients.fdsn import Client
from obspy.clients.fdsn.header import FDSNNoDataException, FDSNException

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations
from lib.logger import setup_logging
from lib.db import DownloadDB


# ---------------------------------------------------------------------------
# Configuration defaults (ingestion-specific)
# ---------------------------------------------------------------------------

DEFAULTS = {
    "fdsnws_url": "https://data.raspberryshake.org",
    "stations_file": "stations.json",
    "chunk_hours": 1,
    "data_latency_hours": 6,
    "fetch_window_hours": 24,
    "max_retries": 3,
    "retry_base_delay_seconds": 10,
    "metadata_refresh_days": 7,
    "output_dir": "output",
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
    "default_channels": "EHZ",
    "default_location": "00",
    "station_delay_seconds": 2,
    "max_workers": 4,
}


# ---------------------------------------------------------------------------
# Station metadata
# ---------------------------------------------------------------------------

def fetch_station_metadata(client, network, station, channels, config, logger):
    """
    Fetch StationXML (with response) and cache locally.
    Returns an Inventory, or None on failure.
    """
    from obspy import read_inventory

    metadata_dir = Path(config["output_dir"]) / "1-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    xml_path = metadata_dir / f"{network}.{station}.xml"

    refresh_days = config["metadata_refresh_days"]

    # Return cached copy if fresh enough
    if xml_path.exists():
        age_days = (time.time() - xml_path.stat().st_mtime) / 86400
        if age_days < refresh_days:
            logger.debug(
                "Using cached metadata for %s.%s (%.1f days old)",
                network, station, age_days,
            )
            return read_inventory(str(xml_path))

    logger.info("Fetching metadata for %s.%s (channels=%s)", network, station, channels)
    try:
        inv = client.get_stations(
            network=network, station=station,
            channel=channels, level="response",
        )
        inv.write(str(xml_path), format="STATIONXML")
        logger.info("Cached metadata -> %s", xml_path)
        return inv
    except Exception as exc:
        logger.error("Metadata fetch failed for %s.%s: %s", network, station, exc)
        if xml_path.exists():
            logger.warning("Falling back to stale cache for %s.%s", network, station)
            return read_inventory(str(xml_path))
        return None


# ---------------------------------------------------------------------------
# Waveform fetch with retry
# ---------------------------------------------------------------------------

def fetch_waveforms(client, network, station, location, channels,
                    starttime, endtime, config, logger):
    """
    Request waveforms with exponential-backoff retry.
    Returns a Stream (possibly empty if no data), or raises on persistent failure.
    """
    max_retries = config["max_retries"]
    base_delay = config["retry_base_delay_seconds"]

    for attempt in range(max_retries + 1):
        try:
            st = client.get_waveforms(
                network=network, station=station,
                location=location, channel=channels,
                starttime=starttime, endtime=endtime,
            )
            return st

        except FDSNNoDataException:
            logger.info(
                "No data available: %s.%s.%s.%s %s - %s",
                network, station, location, channels, starttime, endtime,
            )
            return Stream()

        except (FDSNException, Exception) as exc:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Attempt %d/%d failed for %s.%s (%s). Retrying in %ds...",
                    attempt + 1, max_retries + 1, network, station, exc, delay,
                )
                time.sleep(delay)
            else:
                raise


# ---------------------------------------------------------------------------
# miniSEED storage
# ---------------------------------------------------------------------------

def save_waveforms(stream, output_dir, logger):
    """
    Write a Stream to miniSEED files organised by julian day.

    Directory layout:
        {output_dir}/1-raw/{year}/{julday}/{net}.{sta}.{loc}.{cha}.{year}.{julday}.mseed

    If a day-file already exists, new data is merged into it.
    Returns a list of file paths written.
    """
    if len(stream) == 0:
        return []

    # Group trace slices by (net, sta, loc, cha, year, julday)
    day_bins = {}

    for trace in stream:
        stats = trace.stats
        t0 = stats.starttime
        t1 = stats.endtime

        # Walk through each calendar day the trace spans
        day = UTCDateTime(t0.year, t0.month, t0.day)
        while day < t1:
            next_day = day + 86400
            tr = trace.slice(
                starttime=max(day, t0),
                endtime=min(next_day, t1),
            )
            if tr is not None and tr.stats.npts > 0:
                key = (stats.network, stats.station, stats.location,
                       stats.channel, day.year, day.julday)
                day_bins.setdefault(key, Stream()).append(tr)
            day = next_day

    filepaths = []
    raw_dir = Path(output_dir) / "1-raw"

    for (net, sta, loc, cha, year, jday), st in day_bins.items():
        jday_str = f"{jday:03d}"
        year_str = str(year)

        day_dir = raw_dir / year_str / jday_str
        day_dir.mkdir(parents=True, exist_ok=True)

        fname = f"{net}.{sta}.{loc}.{cha}.{year_str}.{jday_str}.mseed"
        fpath = day_dir / fname

        # Merge with existing day-file
        if fpath.exists():
            try:
                existing = read(str(fpath))
                st = existing + st
            except Exception as exc:
                logger.warning("Could not read existing %s (%s); overwriting", fpath, exc)

        st.merge(method=1, fill_value=0)
        st.write(str(fpath), format="MSEED")

        npts = sum(tr.stats.npts for tr in st)
        filepaths.append(str(fpath))
        logger.info("Wrote %s (%d samples)", fpath, npts)

    return filepaths


# ---------------------------------------------------------------------------
# Per-station processing
# ---------------------------------------------------------------------------

def process_station(client, db, station_cfg, start_time, end_time,
                    config, logger):
    """Fetch metadata and waveforms for one station across the time window."""
    network = station_cfg["network"]
    station = station_cfg["station"]
    location = station_cfg.get("location", config["default_location"])
    channels = station_cfg.get("channels", config["default_channels"])

    logger.info(
        "--- %s.%s (channels=%s) | %s -> %s ---",
        network, station, channels, start_time, end_time,
    )

    # Station metadata (StationXML with response)
    inv = fetch_station_metadata(client, network, station, channels, config, logger)
    if inv is None:
        logger.error("Skipping %s.%s: no metadata available", network, station)
        return {"success": 0, "no_data": 0, "failed": 0, "skipped": 0}

    # Walk through time in chunks
    chunk_sec = config["chunk_hours"] * 3600
    chunk_start = UTCDateTime(start_time)
    chunk_end_limit = UTCDateTime(end_time)
    total_chunks = int((chunk_end_limit - chunk_start) / chunk_sec) + 1

    counts = {"success": 0, "no_data": 0, "failed": 0, "skipped": 0}
    chunk_num = 0

    while chunk_start < chunk_end_limit:
        chunk_end = min(chunk_start + chunk_sec, chunk_end_limit)
        chunk_num += 1

        # Already downloaded?
        if db.is_downloaded(network, station, location, channels,
                            chunk_start, chunk_end):
            logger.debug(
                "  [%d/%d] Already downloaded %s -> %s",
                chunk_num, total_chunks, chunk_start, chunk_end,
            )
            counts["skipped"] += 1
            chunk_start = chunk_end
            continue

        logger.info(
            "  [%d/%d] Fetching %s -> %s",
            chunk_num, total_chunks, chunk_start, chunk_end,
        )

        try:
            st = fetch_waveforms(
                client, network, station, location, channels,
                chunk_start, chunk_end, config, logger,
            )

            if len(st) == 0:
                db.record(network, station, location, channels,
                          chunk_start, chunk_end, "no_data")
                counts["no_data"] += 1
            else:
                filepaths = save_waveforms(st, config["output_dir"], logger)
                db.record(network, station, location, channels,
                          chunk_start, chunk_end, "success", filepaths)
                counts["success"] += 1

        except Exception as exc:
            logger.error(
                "  Failed %s.%s %s -> %s: %s",
                network, station, chunk_start, chunk_end, exc,
            )
            db.record(network, station, location, channels,
                      chunk_start, chunk_end, "failed", error=str(exc))
            counts["failed"] += 1

        chunk_start = chunk_end

        # Throttle requests to avoid rate limiting
        delay = config["station_delay_seconds"]
        if delay > 0 and chunk_start < chunk_end_limit:
            time.sleep(delay)

    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Ingest seismic waveforms from Raspberry Shake FDSNWS",
    )
    parser.add_argument("--config", required=True,
                        help="Path to YAML configuration file")
    parser.add_argument("--start",
                        help="Backfill start (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end",
                        help="Backfill end (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging")
    return parser.parse_args()


def main():
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("ingest", config, debug=args.debug)

    logger.info("=" * 60)
    logger.info("Seismic data ingestion starting")
    logger.info("=" * 60)

    # Determine time window
    if args.start and args.end:
        start_time = UTCDateTime(args.start)
        end_time = UTCDateTime(args.end)
        logger.info("Backfill mode: %s -> %s", start_time, end_time)
    elif args.start or args.end:
        logger.error("Both --start and --end are required for backfill mode")
        sys.exit(1)
    else:
        end_time = UTCDateTime() - config["data_latency_hours"] * 3600
        start_time = end_time - config["fetch_window_hours"] * 3600
        logger.info("Scheduled mode: %s -> %s", start_time, end_time)

    # FDSNWS clients (cached by URL — stations may use different servers)
    clients = {}
    failed_urls = set()
    default_url = config["fdsnws_url"]

    connect_retries = config.get("connect_retries", 3)
    connect_delay = config.get("connect_retry_delay_seconds", 30)

    client_lock = threading.Lock()

    def get_client(url):
        with client_lock:
            if url in failed_urls:
                return None
            if url in clients:
                return clients[url]
        # Connection attempt outside lock (slow, no dict mutation)
        new_client = None
        for attempt in range(connect_retries + 1):
            try:
                logger.info("Connecting to FDSNWS: %s (attempt %d/%d)",
                            url, attempt + 1, connect_retries + 1)
                new_client = Client(url)
                break
            except Exception as exc:
                if attempt < connect_retries:
                    logger.warning("Connection failed (%s). Retrying in %ds...",
                                   exc, connect_delay)
                    time.sleep(connect_delay)
                else:
                    logger.error("Cannot connect to FDSNWS at %s after %d attempts: %s",
                                 url, connect_retries + 1, exc)
                    with client_lock:
                        failed_urls.add(url)
                    return None
        with client_lock:
            # Another thread may have connected while we were trying
            if url in clients:
                return clients[url]
            clients[url] = new_client
        return new_client

    db = DownloadDB(config["output_dir"])

    try:
        max_workers = config["max_workers"]
        logger.info("Processing %d station(s) with %d worker(s)", len(stations), max_workers)

        totals = {"success": 0, "no_data": 0, "failed": 0, "skipped": 0}

        def fetch_station(station_cfg):
            url = station_cfg.get("fdsnws_url", default_url)
            client = get_client(url)
            if client is None:
                logger.warning(
                    "Skipping %s.%s — FDSNWS at %s unavailable",
                    station_cfg["network"], station_cfg["station"], url,
                )
                return {"success": 0, "no_data": 0, "failed": 0, "skipped": 0}
            return process_station(
                client, db, station_cfg, start_time, end_time, config, logger,
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_station, s): s for s in stations}
            for future in as_completed(futures):
                station_cfg = futures[future]
                try:
                    counts = future.result()
                    for k in totals:
                        totals[k] += counts[k]
                except Exception as exc:
                    logger.error("Station %s.%s raised: %s",
                                 station_cfg["network"], station_cfg["station"], exc)

        # Summary
        logger.info("=" * 60)
        logger.info(
            "Done. success=%d  no_data=%d  failed=%d  skipped=%d",
            totals["success"], totals["no_data"],
            totals["failed"], totals["skipped"],
        )
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        db.close()


if __name__ == "__main__":
    main()
