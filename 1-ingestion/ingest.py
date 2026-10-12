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

from __future__ import annotations

import argparse
import fnmatch
import gc
import math
import sys
import threading
import time
from datetime import datetime, timezone
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from obspy import Stream, UTCDateTime, read
from obspy.clients.fdsn import Client
from obspy.clients.fdsn.header import FDSNException, FDSNNoDataException

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.atomic_io import atomic_write_stream
from lib.config import load_config, load_stations
from lib.constants import CIRCUIT_BREAKER_BACKOFF_SECONDS, CIRCUIT_BREAKER_THRESHOLD
from lib.db import DownloadDB
from lib.logger import MetricsWriter, setup_logging

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
    # A "success" chunk with fewer than this fraction of the expected samples
    # counts as short: the run exits EXIT_PARTIAL and the chunk is re-requested
    # on later runs while it is younger than refetch_window_days (Raspberry
    # Shakes upload late after outages). no_data chunks follow the same window.
    "min_chunk_coverage": 0.98,
    "refetch_window_days": 7,
}


# ---------------------------------------------------------------------------
# FDSNWS circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Tracks consecutive 503 errors per host and triggers backoff.

    Thread-safe. When the threshold is hit, all requests to that host
    block for the backoff duration before retrying.
    """

    def __init__(
        self,
        threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        backoff_seconds: float = CIRCUIT_BREAKER_BACKOFF_SECONDS,
    ) -> None:
        self._threshold = threshold
        self._backoff = backoff_seconds
        self._counts: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, host: str) -> None:
        with self._lock:
            self._counts[host] = 0

    def record_failure(self, host: str, logger) -> None:
        with self._lock:
            self._counts[host] = self._counts.get(host, 0) + 1
            count = self._counts[host]
            if count >= self._threshold:
                until = time.monotonic() + self._backoff
                self._open_until[host] = until
                self._counts[host] = 0
                logger.warning(
                    "Circuit breaker OPEN for %s: %d consecutive 503s, backing off %.0fs",
                    host,
                    count,
                    self._backoff,
                )

    def check(self, host: str, logger) -> None:
        """Block if circuit is open for this host."""
        with self._lock:
            until = self._open_until.get(host, 0)
        if until > 0:
            wait = until - time.monotonic()
            if wait > 0:
                logger.info("Circuit breaker: waiting %.0fs for %s", wait, host)
                time.sleep(wait)
                with self._lock:
                    self._open_until.pop(host, None)


# Module-level circuit breaker shared across threads
_circuit_breaker = CircuitBreaker()


# ---------------------------------------------------------------------------
# Station metadata
# ---------------------------------------------------------------------------


# ── Exit codes ───────────────────────────────────────────────────────────
# The orchestrator keys on these. 0: every requested chunk is either stored
# or genuinely has no data. 2: some data was stored but some chunks failed or
# some stations were unreachable — the day can proceed, but it is incomplete
# and must be re-ingested later. 1: nothing was obtained although something
# was requested (server outage) — retry the whole window.
EXIT_OK = 0
EXIT_TOTAL_FAILURE = 1
EXIT_PARTIAL = 2


SUMMARY_FILENAME = "ingest_summary.json"


def write_run_summary(config, start_time, end_time, totals, n_unreachable, exit_code) -> None:
    """Machine-readable result of this run for the orchestrator (logs/ingest_summary.json)."""
    import json

    from lib.atomic_io import atomic_write_text

    summary = {
        "start": str(start_time),
        "end": str(end_time),
        "totals": totals,
        "stations_unreachable": n_unreachable,
        "exit_code": exit_code,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    atomic_write_text(json.dumps(summary, indent=2) + "\n", Path(config["log_dir"]) / SUMMARY_FILENAME)


def exit_code_for(totals: dict[str, int], n_unreachable: int, n_stations: int) -> int:
    """Map the run's counters to the exit-code policy above."""
    problems = totals["failed"] > 0 or n_unreachable > 0 or totals.get("short", 0) > 0
    if not problems:
        return EXIT_OK
    obtained = totals["success"] + totals["no_data"] + totals["skipped"]
    if obtained == 0 or n_unreachable >= n_stations:
        return EXIT_TOTAL_FAILURE
    return EXIT_PARTIAL


def expected_channels(inv, network: str, station: str, location: str, channels: str) -> dict[str, float]:
    """Channel code -> sample rate for the inventory channels this request covers.

    Derived from the StationXML we already fetch, so an RS4D ("E??" = EHZ +
    EN?) and an RS3D ("EH?") are handled without any station-type logic.
    """
    out: dict[str, float] = {}
    for net in inv:
        if net.code != network:
            continue
        for sta in net:
            if sta.code != station:
                continue
            for cha in sta:
                if (cha.location_code or "") != (location or ""):
                    continue
                if fnmatch.fnmatch(cha.code, channels) and cha.sample_rate:
                    out[cha.code] = float(cha.sample_rate)
    return out


def chunk_coverage(st: Stream, expected: dict[str, float], chunk_start, chunk_end) -> float:
    """Fraction of the requested samples present in ``st`` (0..1).

    Per expected channel: samples received / samples a full chunk holds,
    capped at 1; averaged over the expected channels. Channels absent from
    the inventory but present in the data are ignored; with no expectation
    at all, every returned channel counts as expected.
    """
    duration = float(chunk_end - chunk_start)
    if duration <= 0:
        return 1.0
    if not expected:
        expected = {}
        for tr in st:
            expected.setdefault(tr.stats.channel, float(tr.stats.sampling_rate))
    if not expected:
        return 0.0
    total = 0.0
    for cha, rate in expected.items():
        got = sum(tr.stats.npts for tr in st if tr.stats.channel == cha)
        rates = [tr.stats.sampling_rate for tr in st if tr.stats.channel == cha]
        rate = float(rates[0]) if rates else rate
        want = duration * rate
        total += min(got / want, 1.0) if want > 0 else 0.0
    return round(total / len(expected), 4)


def chunk_improved(previous: tuple[str, float | None] | None, coverage: float) -> bool:
    """True when this fetch stored more than what the tracker had before.

    First-time fetches and recoveries from failed/no_data count; a re-fetch
    that returns the same short data does not. The orchestrator uses the
    total to decide whether a partial day is worth re-processing.
    """
    if previous is None:
        return True
    status, old_cov = previous
    if status != "success":
        return True
    if old_cov is None:  # legacy row, treated as complete
        return False
    return coverage > old_cov + 1e-3


def fetch_station_metadata(
    client: Client,
    network: str,
    station: str,
    channels: str,
    config: dict,
    logger,
):
    """Fetch StationXML (with response) and cache locally.

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
                network,
                station,
                age_days,
            )
            return read_inventory(str(xml_path))

    logger.info("Fetching metadata for %s.%s (channels=%s)", network, station, channels)
    try:
        inv = client.get_stations(
            network=network,
            station=station,
            channel=channels,
            level="response",
        )
        inv.write(str(xml_path), format="STATIONXML")
        logger.info("Cached metadata -> %s", xml_path)
        return inv
    except FDSNException as exc:
        logger.error(
            "Metadata fetch failed for %s.%s: %s [%s]",
            network,
            station,
            exc,
            type(exc).__name__,
        )
        if xml_path.exists():
            logger.warning("Falling back to stale cache for %s.%s", network, station)
            return read_inventory(str(xml_path))
        return None
    except OSError as exc:
        logger.error(
            "Network error fetching metadata for %s.%s: %s",
            network,
            station,
            exc,
        )
        if xml_path.exists():
            logger.warning("Falling back to stale cache for %s.%s", network, station)
            return read_inventory(str(xml_path))
        return None


# ---------------------------------------------------------------------------
# Waveform fetch with retry
# ---------------------------------------------------------------------------


def fetch_waveforms(
    client: Client,
    network: str,
    station: str,
    location: str,
    channels: str,
    starttime: UTCDateTime,
    endtime: UTCDateTime,
    config: dict,
    logger,
) -> Stream:
    """Request waveforms with exponential-backoff retry.

    Returns a Stream (possibly empty if no data), or raises on persistent failure.
    """
    max_retries = config["max_retries"]
    base_delay = config["retry_base_delay_seconds"]
    host = config.get("fdsnws_url", "unknown")

    for attempt in range(max_retries + 1):
        # Check circuit breaker before each attempt
        _circuit_breaker.check(host, logger)

        try:
            st = client.get_waveforms(
                network=network,
                station=station,
                location=location,
                channel=channels,
                starttime=starttime,
                endtime=endtime,
            )
            _circuit_breaker.record_success(host)
            return st

        except FDSNNoDataException:
            logger.info(
                "No data available: %s.%s.%s.%s %s - %s",
                network,
                station,
                location,
                channels,
                starttime,
                endtime,
            )
            return Stream()

        except FDSNException as exc:
            # Track 503s and 429s for circuit breaker
            exc_str = str(exc)
            if "503" in exc_str or "Service Unavailable" in exc_str:
                _circuit_breaker.record_failure(host, logger)
            if "429" in exc_str or "Too Many Requests" in exc_str:
                _circuit_breaker.record_failure(host, logger)

            if attempt < max_retries:
                # Use longer backoff for rate limiting (429)
                if "429" in exc_str or "Too Many Requests" in exc_str:
                    delay = base_delay * (4**attempt)
                else:
                    delay = base_delay * (2**attempt)
                logger.warning(
                    "Attempt %d/%d failed for %s.%s (%s: %s). Retrying in %ds...",
                    attempt + 1,
                    max_retries + 1,
                    network,
                    station,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "All %d attempts exhausted for %s.%s %s-%s: %s\n%s",
                    max_retries + 1,
                    network,
                    station,
                    starttime,
                    endtime,
                    exc,
                    traceback.format_exc(),
                )
                raise

        except (ConnectionError, OSError) as exc:
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "Network error for %s.%s (%s: %s). Retrying in %ds...",
                    network,
                    station,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                raise

    # Should not reach here, but satisfy type checker
    return Stream()


# ---------------------------------------------------------------------------
# miniSEED storage
# ---------------------------------------------------------------------------


def _is_encoding_error(exc: BaseException) -> bool:
    """True when libmseed refused to pack the samples (surfaces as a bare Exception)."""
    msg = str(exc).lower()
    return "steim" in msg or "packing data samples" in msg or "encode" in msg


def write_day_file(st: Stream, fpath: Path, logger) -> None:
    """Write a merged day-file, falling back to a lossless encoding if Steim2 cannot.

    Raspberry Shake occasionally serves a record that fails its Steim2
    integrity check. After merging, consecutive samples can then differ by
    more than Steim2's 30-bit limit and libmseed raises "Unable to represent
    difference in <= 30 bits". Before this fallback that exception escaped the
    worker thread and killed the whole ingest run (every station, every
    retry). INT32/FLOAT64 can represent any sample, so the data is kept and
    the failure is confined to a log warning.
    """
    try:
        atomic_write_stream(st, fpath, format="MSEED")
        return
    except Exception as exc:
        if not _is_encoding_error(exc):
            raise
        logger.warning(
            "Steim2 encoding failed for %s (%s); rewriting with a lossless integer/float encoding",
            fpath, str(exc).strip().splitlines()[-1] if str(exc).strip() else type(exc).__name__,
        )

    fallback = Stream()
    for tr in st:
        tr = tr.copy()
        kind, size = tr.data.dtype.kind, tr.data.dtype.itemsize
        if kind == "i" and size <= 2:
            enc = "INT16"
        elif kind == "i" and size == 4:
            enc = "INT32"
        else:
            tr.data = tr.data.astype("float64")
            enc = "FLOAT64"
        if "mseed" not in tr.stats:
            tr.stats.mseed = {}
        tr.stats.mseed["encoding"] = enc
        fallback.append(tr)
    atomic_write_stream(fallback, fpath, format="MSEED")


def save_waveforms(stream: Stream, output_dir: str, logger) -> list[str]:
    """Write a Stream to miniSEED files organised by julian day.

    Directory layout:
        {output_dir}/1-raw/{year}/{julday}/{net}.{sta}.{loc}.{cha}.{year}.{julday}.mseed

    If a day-file already exists, new data is merged into it.
    Returns a list of file paths written.
    """
    if len(stream) == 0:
        return []

    # Group trace slices by (net, sta, loc, cha, year, julday)
    day_bins: dict[tuple, Stream] = {}

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
                key = (
                    stats.network,
                    stats.station,
                    stats.location,
                    stats.channel,
                    day.year,
                    day.julday,
                )
                day_bins.setdefault(key, Stream()).append(tr)
            day = next_day

    filepaths: list[str] = []
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

        # Store gaps AS gaps. fill_value=0 used to write zeros into the raw
        # archive, which later stages (merge(fill_value="interpolate")) could
        # no longer tell from data, so zeros reached PhaseNet and ML. merge()
        # without fill_value leaves masked arrays; split() turns them back
        # into one contiguous trace per segment, which miniSEED stores natively.
        st.merge(method=1)
        st = st.split()
        write_day_file(st, fpath, logger)

        npts = sum(tr.stats.npts for tr in st)
        filepaths.append(str(fpath))
        logger.info("Wrote %s (%d samples)", fpath, npts)

    return filepaths


# ---------------------------------------------------------------------------
# Per-station processing
# ---------------------------------------------------------------------------


def process_station(
    client: Client,
    db: DownloadDB,
    station_cfg: dict,
    start_time: UTCDateTime,
    end_time: UTCDateTime,
    config: dict,
    logger,
) -> dict[str, int]:
    """Fetch metadata and waveforms for one station across the time window."""
    network = station_cfg["network"]
    station = station_cfg["station"]
    location = station_cfg.get("location", config["default_location"])
    channels = station_cfg.get("channels", config["default_channels"])
    empty_counts = {"success": 0, "no_data": 0, "failed": 0, "skipped": 0}

    # Skip stations that weren't online during this entire window. Without
    # this, gap-fill on an early date would loop through every chunk for a
    # not-yet-deployed station, eating the 5 s polite delay per chunk for
    # nothing (FDSNWS returns "no data" but the delay still fires). The
    # gap-scan logic in run_pipeline.py already uses start_date; this just
    # propagates that same gate into ingest itself.
    sta_start_raw = station_cfg.get("start_date", "")
    if sta_start_raw:
        try:
            sta_start_dt = UTCDateTime(sta_start_raw)
            if sta_start_dt >= UTCDateTime(end_time):
                logger.info(
                    "Skipping %s.%s -- start_date %s is after window end %s",
                    network,
                    station,
                    sta_start_raw,
                    end_time,
                )
                return empty_counts
        except (ValueError, TypeError):
            pass  # malformed date — fall through and process normally

    logger.info(
        "--- %s.%s (channels=%s) | %s -> %s ---",
        network,
        station,
        channels,
        start_time,
        end_time,
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
    total_chunks = int(math.ceil((chunk_end_limit - chunk_start) / chunk_sec))

    counts: dict[str, int] = {
        "success": 0, "no_data": 0, "failed": 0, "skipped": 0, "short": 0, "improved": 0,
    }
    chunk_num = 0
    expected = expected_channels(inv, network, station, location, channels)
    min_cov = float(config["min_chunk_coverage"])
    refetch_cutoff = UTCDateTime() - float(config["refetch_window_days"]) * 86400.0
    force = bool(config.get("_force_refetch", False))

    while chunk_start < chunk_end_limit:
        chunk_end = min(chunk_start + chunk_sec, chunk_end_limit)
        chunk_num += 1

        # Already downloaded (complete), or short/no-data but too old to retry?
        if not db.needs_fetch(
            network, station, location, channels, chunk_start, chunk_end,
            min_coverage=min_cov,
            within_refetch_window=chunk_end >= refetch_cutoff,
            force=force,
        ):
            logger.debug(
                "  [%d/%d] Already downloaded %s -> %s",
                chunk_num,
                total_chunks,
                chunk_start,
                chunk_end,
            )
            counts["skipped"] += 1
            chunk_start = chunk_end
            continue

        logger.info(
            "  [%d/%d] Fetching %s -> %s",
            chunk_num,
            total_chunks,
            chunk_start,
            chunk_end,
        )
        previous = db.chunk_status(network, station, location, channels, chunk_start, chunk_end)

        try:
            st = fetch_waveforms(
                client,
                network,
                station,
                location,
                channels,
                chunk_start,
                chunk_end,
                config,
                logger,
            )

            if len(st) == 0:
                db.record(network, station, location, channels, chunk_start, chunk_end, "no_data")
                counts["no_data"] += 1
            else:
                filepaths = save_waveforms(st, config["output_dir"], logger)
                coverage = chunk_coverage(st, expected, chunk_start, chunk_end)
                db.record(
                    network,
                    station,
                    location,
                    channels,
                    chunk_start,
                    chunk_end,
                    "success",
                    filepaths,
                    coverage=coverage,
                )
                counts["success"] += 1
                if chunk_improved(previous, coverage):
                    counts["improved"] += 1
                if coverage < min_cov:
                    counts["short"] += 1
                    logger.warning(
                        "  short chunk %s.%s %s -> %s: coverage %.1f%% (< %.0f%%); "
                        "will re-request while younger than %s days",
                        network, station, chunk_start, chunk_end,
                        coverage * 100, min_cov * 100, config["refetch_window_days"],
                    )

        except FDSNException as exc:
            logger.error(
                "  FDSN error %s.%s %s -> %s: %s: %s",
                network,
                station,
                chunk_start,
                chunk_end,
                type(exc).__name__,
                exc,
            )
            db.record(
                network,
                station,
                location,
                channels,
                chunk_start,
                chunk_end,
                "failed",
                error=str(exc),
            )
            counts["failed"] += 1

        except (ConnectionError, OSError) as exc:
            logger.error(
                "  Network error %s.%s %s -> %s: %s: %s",
                network,
                station,
                chunk_start,
                chunk_end,
                type(exc).__name__,
                exc,
            )
            db.record(
                network,
                station,
                location,
                channels,
                chunk_start,
                chunk_end,
                "failed",
                error=str(exc),
            )
            counts["failed"] += 1

        except Exception as exc:  # noqa: BLE001 - one bad chunk must not kill the run
            # Anything else (corrupt miniSEED, encoder failure, library bug)
            # used to propagate out of the worker thread and abort ingest for
            # every station. Record it as a failed chunk so the orchestrator
            # sees a partial day and retries just this chunk later.
            logger.error(
                "  Unexpected error %s.%s %s -> %s: %s: %s\n%s",
                network,
                station,
                chunk_start,
                chunk_end,
                type(exc).__name__,
                exc,
                traceback.format_exc(),
            )
            db.record(
                network,
                station,
                location,
                channels,
                chunk_start,
                chunk_end,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            counts["failed"] += 1

        chunk_start = chunk_end

        # Throttle requests to avoid rate limiting
        delay = config["station_delay_seconds"]
        if delay > 0 and chunk_start < chunk_end_limit:
            time.sleep(delay)

    # Explicit cleanup after processing a station
    gc.collect()

    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest seismic waveforms from Raspberry Shake FDSNWS",
    )
    parser.add_argument("--config", required=True, help="Path to YAML configuration file")
    parser.add_argument("--start", help="Backfill start (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end", help="Backfill end (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-request short and no-data chunks regardless of age (complete "
        "chunks already on disk are never re-downloaded).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    config["_force_refetch"] = bool(args.force)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("ingest", config, debug=args.debug)
    metrics = MetricsWriter(Path(config["log_dir"]) / "metrics.jsonl")

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

    # FDSNWS clients (cached by URL -- stations may use different servers)
    clients: dict[str, Client] = {}
    failed_urls: set[str] = set()
    default_url = config["fdsnws_url"]

    connect_retries = config.get("connect_retries", 3)
    connect_delay = config.get("connect_retry_delay_seconds", 30)

    client_lock = threading.Lock()

    def get_client(url: str) -> Client | None:
        with client_lock:
            if url in failed_urls:
                return None
            if url in clients:
                return clients[url]
        # Connection attempt outside lock (slow, no dict mutation)
        new_client = None
        for attempt in range(connect_retries + 1):
            try:
                logger.info(
                    "Connecting to FDSNWS: %s (attempt %d/%d)",
                    url,
                    attempt + 1,
                    connect_retries + 1,
                )
                new_client = Client(url)
                break
            except FDSNException as exc:
                if attempt < connect_retries:
                    logger.warning(
                        "FDSN connection failed (%s: %s). Retrying in %ds...",
                        type(exc).__name__,
                        exc,
                        connect_delay,
                    )
                    time.sleep(connect_delay)
                else:
                    logger.error(
                        "Cannot connect to FDSNWS at %s after %d attempts: %s",
                        url,
                        connect_retries + 1,
                        exc,
                    )
                    with client_lock:
                        failed_urls.add(url)
                    return None
            except (ConnectionError, OSError) as exc:
                if attempt < connect_retries:
                    logger.warning(
                        "Network error connecting (%s: %s). Retrying in %ds...",
                        type(exc).__name__,
                        exc,
                        connect_delay,
                    )
                    time.sleep(connect_delay)
                else:
                    logger.error(
                        "Cannot connect to FDSNWS at %s after %d attempts: %s",
                        url,
                        connect_retries + 1,
                        exc,
                    )
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
    exit_code = EXIT_OK

    try:
        max_workers = config["max_workers"]
        logger.info("Processing %d station(s) with %d worker(s)", len(stations), max_workers)

        totals: dict[str, int] = {
            "success": 0, "no_data": 0, "failed": 0, "skipped": 0, "short": 0, "improved": 0,
        }
        n_stations_unreachable = 0

        def fetch_station(station_cfg: dict) -> dict[str, int]:
            nonlocal n_stations_unreachable
            url = station_cfg.get("fdsnws_url", default_url)
            client = get_client(url)
            if client is None:
                logger.warning(
                    "Skipping %s.%s -- FDSNWS at %s unavailable",
                    station_cfg["network"],
                    station_cfg["station"],
                    url,
                )
                n_stations_unreachable += 1
                return {"success": 0, "no_data": 0, "failed": 0, "skipped": 0}
            t0 = time.monotonic()
            result = process_station(
                client,
                db,
                station_cfg,
                start_time,
                end_time,
                config,
                logger,
            )
            elapsed = time.monotonic() - t0
            metrics.record(
                "ingest",
                station=f"{station_cfg['network']}.{station_cfg['station']}",
                duration_s=round(elapsed, 1),
                **result,
            )
            return result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_station, s): s for s in stations}
            for future in as_completed(futures):
                station_cfg = futures[future]
                try:
                    counts = future.result()
                    for k in totals:
                        totals[k] += counts.get(k, 0)
                except FDSNException as exc:
                    n_stations_unreachable += 1
                    logger.error(
                        "Station %s.%s FDSN error: %s: %s",
                        station_cfg["network"],
                        station_cfg["station"],
                        type(exc).__name__,
                        exc,
                    )
                except (ConnectionError, OSError) as exc:
                    n_stations_unreachable += 1
                    logger.error(
                        "Station %s.%s network error: %s: %s",
                        station_cfg["network"],
                        station_cfg["station"],
                        type(exc).__name__,
                        exc,
                    )

        # Summary
        logger.info("=" * 60)
        logger.info(
            "Done. success=%d  no_data=%d  failed=%d  skipped=%d  short=%d  improved=%d",
            totals["success"],
            totals["no_data"],
            totals["failed"],
            totals["skipped"],
            totals["short"],
            totals["improved"],
        )
        logger.info("=" * 60)
        exit_code = exit_code_for(totals, n_stations_unreachable, len(stations))
        write_run_summary(config, start_time, end_time, totals, n_stations_unreachable, exit_code)
        if exit_code == EXIT_TOTAL_FAILURE:
            logger.error(
                "No data obtained for any station (failed=%d, unreachable=%d) — "
                "exiting %d so the orchestrator retries this window.",
                totals["failed"], n_stations_unreachable, exit_code,
            )
        elif exit_code == EXIT_PARTIAL:
            logger.warning(
                "%d chunk(s) failed, %d short, %d station(s) unreachable; partial data "
                "written. Exiting %d so the orchestrator records the day as incomplete.",
                totals["failed"], totals["short"], n_stations_unreachable, exit_code,
            )

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        exit_code = 130
    finally:
        db.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
