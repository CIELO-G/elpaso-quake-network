#!/usr/bin/env python3
"""
detect.py - Seismic phase detection and picking

Step 3/5 of the El Paso seismic processing pipeline.
Runs PhaseNet (via SeisBench) on preprocessed waveforms to identify
P- and S-wave arrivals.

Usage (run from project root):
    python 3-detection/detect.py --config 3-detection/config.yaml
    python 3-detection/detect.py --config 3-detection/config.yaml --start 2026-01-29 --end 2026-01-29
    python 3-detection/detect.py --config 3-detection/config.yaml --force --debug
"""

from __future__ import annotations

import argparse
import csv
import gc
import sys
import time
from pathlib import Path

import numpy as np
from obspy import UTCDateTime, Stream, read

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import load_config, load_stations
from lib.logger import MetricsWriter, setup_logging

DEFAULTS = {
    "stations_file": "stations.json",
    "input_dir": "output",
    "output_dir": "output",
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
    "phasenet_model": "original",
    "p_threshold": 0.3,
    "s_threshold": 0.3,
    "classify_overlap": 1500,
    "classify_batch_size": 256,
    "detect_window_hours": 24,
    "detect_latency_hours": 6,
    # Highpass pre-filter before PhaseNet (Hz); set to 0 or null to disable
    "highpass_freq": 3.0,
    # Phase-dependent amplitude windows (SCIENCE_AUDIT.md Issue 7)
    "amp_window_p_before": 0.5,
    "amp_window_p_after": 2.0,
    "amp_window_s_before": 0.5,
    "amp_window_s_after": 5.0,
}

CSV_COLUMNS = [
    "network", "station", "location", "channel",
    "phase", "time", "probability", "model",
    "amplitude", "amplitude_channel",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect and pick seismic phases from preprocessed waveforms",
    )
    parser.add_argument("--config", required=True,
                        help="Path to YAML configuration file")
    parser.add_argument("--start",
                        help="Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end",
                        help="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--force", action="store_true",
                        help="Redetect even if output CSV already exists")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_phasenet_model(config: dict, logger):
    """Load PhaseNet once via SeisBench.  Imports torch/seisbench here
    so that ``--help`` stays fast without torch installed."""
    try:
        import torch
        import seisbench.models as sbm
    except ImportError as exc:
        logger.critical("Cannot import seisbench/torch: %s", exc)
        sys.exit(1)

    pretrained = config["phasenet_model"]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading PhaseNet (weights=%s) on %s", pretrained, device)

    try:
        model = sbm.PhaseNet.from_pretrained(pretrained)
        model.to(device)
        logger.info("PhaseNet loaded successfully")
        return model
    except Exception as exc:
        logger.critical("Failed to load PhaseNet model: %s: %s",
                        type(exc).__name__, exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Channel logic
# ---------------------------------------------------------------------------

def get_detection_channels(station_cfg: dict) -> tuple[str, bool, str | None]:
    """Return ``(channel_pattern, is_3c, fallback_pattern)`` for a station."""
    model = station_cfg.get("model", "")
    channels = station_cfg.get("channels", "")

    if model == "RS4D":
        return ("EN?", True, "EHZ")
    if channels == "HH?":
        return ("HH?", True, None)
    if channels == "EH?":
        return ("EH?", True, None)
    # Fallback: use whatever channels are configured, assume 1C
    return (channels, False, None)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_processed_files(
    station_cfg: dict,
    input_dir: str,
    year: str,
    jday: str,
    channel_pattern: str,
) -> list[Path]:
    """Glob for processed miniSEED files matching a station/day/channel pattern."""
    proc_dir = Path(input_dir) / "2-processed" / year / jday
    if not proc_dir.is_dir():
        return []

    net = station_cfg["network"]
    sta = station_cfg["station"]
    loc = station_cfg.get("location", "00")

    pattern = f"{net}.{sta}.{loc}.{channel_pattern}.{year}.{jday}.mseed"
    return sorted(proc_dir.glob(pattern))


def get_daily_picks_path(output_dir: str, year: str, jday: str) -> Path:
    """Return the CSV path for a day's picks."""
    return Path(output_dir) / "3-picks" / year / jday / f"{year}.{jday}.picks.csv"


# ---------------------------------------------------------------------------
# Waveform I/O
# ---------------------------------------------------------------------------

def load_waveforms(file_paths: list[Path], logger) -> Stream:
    """Read miniSEED files into a single merged Stream."""
    st = Stream()
    for fp in file_paths:
        try:
            st += read(str(fp))
        except Exception as exc:
            logger.warning("Cannot read %s: %s: %s", fp, type(exc).__name__, exc)
    if len(st) > 0:
        st.merge(method=1, fill_value=0)
    return st


# ---------------------------------------------------------------------------
# Pick extraction
# ---------------------------------------------------------------------------

def extract_picks(model, stream: Stream, config: dict, logger) -> list[dict]:
    """Run PhaseNet classification and return a list of pick dicts."""
    model_tag = f"PhaseNet:{config['phasenet_model']}"

    try:
        picks = model.classify(
            stream,
            P_threshold=config["p_threshold"],
            S_threshold=config["s_threshold"],
            overlap=config["classify_overlap"],
            batch_size=config["classify_batch_size"],
        )
    except RuntimeError as exc:
        logger.error("model.classify() runtime error: %s: %s",
                      type(exc).__name__, exc)
        return []
    except Exception as exc:
        logger.error("model.classify() failed: %s: %s",
                      type(exc).__name__, exc)
        return []

    results: list[dict] = []
    for pick in picks.picks:
        parts = pick.trace_id.split(".")
        net = parts[0] if len(parts) > 0 else ""
        sta = parts[1] if len(parts) > 1 else ""
        loc = parts[2] if len(parts) > 2 else ""

        results.append({
            "network": net,
            "station": sta,
            "location": loc,
            "phase": pick.phase,
            "time": str(pick.peak_time),
            "probability": f"{pick.peak_value:.4f}",
            "model": model_tag,
        })

    return results


# ---------------------------------------------------------------------------
# Amplitude measurement
# ---------------------------------------------------------------------------

def measure_amplitudes(
    picks_list: list[dict],
    stream: Stream,
    config: dict,
    logger,
) -> None:
    """Measure peak absolute amplitude on the velocity trace for each pick.

    Uses phase-dependent windows: P-waves get a shorter window,
    S-waves get a longer window to capture coda peak (SCIENCE_AUDIT.md Issue 7).

    Modifies *picks_list* in place.
    """
    p_before = config["amp_window_p_before"]
    p_after = config["amp_window_p_after"]
    s_before = config["amp_window_s_before"]
    s_after = config["amp_window_s_after"]

    # Build a lookup from station prefix (NET.STA.LOC) -> list of Traces
    station_traces: dict[str, list] = {}
    for tr in stream:
        prefix = f"{tr.stats.network}.{tr.stats.station}.{tr.stats.location}"
        station_traces.setdefault(prefix, []).append(tr)

    for pick in picks_list:
        prefix = f"{pick['network']}.{pick['station']}.{pick['location']}"
        traces = station_traces.get(prefix, [])

        if not traces:
            pick["amplitude"] = ""
            pick["amplitude_channel"] = ""
            pick["channel"] = ""
            continue

        # Prefer vertical (Z) component for amplitude
        tr = next((t for t in traces if t.stats.channel.endswith("Z")), traces[0])
        pick["channel"] = tr.stats.channel

        # Phase-dependent amplitude window
        phase = pick.get("phase", "P")
        if phase == "S":
            before, after = s_before, s_after
        else:
            before, after = p_before, p_after

        pick_time = UTCDateTime(pick["time"])
        t1 = pick_time - before
        t2 = pick_time + after

        try:
            windowed = tr.slice(starttime=t1, endtime=t2)
            if windowed is None or windowed.stats.npts == 0:
                pick["amplitude"] = ""
                pick["amplitude_channel"] = ""
                continue

            peak = float(np.max(np.abs(windowed.data)))
            pick["amplitude"] = f"{peak:.6e}"
            pick["amplitude_channel"] = tr.stats.channel
        except Exception as exc:
            logger.debug("Amplitude measurement failed for %s at %s: %s: %s",
                         prefix, pick["time"], type(exc).__name__, exc)
            pick["amplitude"] = ""
            pick["amplitude_channel"] = ""


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_picks_csv(picks_list: list[dict], output_path: Path, logger) -> None:
    """Write picks to CSV.  Always writes header even if no picks."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for pick in picks_list:
            writer.writerow(pick)
    logger.info("Wrote %d pick(s) to %s", len(picks_list), output_path)


# ---------------------------------------------------------------------------
# Per-station detection (returns picks, does not write)
# ---------------------------------------------------------------------------

def detect_station(
    station_cfg: dict,
    year: str,
    jday: str,
    model,
    config: dict,
    logger,
) -> tuple[list[dict], str]:
    """Run detection for one station on one julian day.

    Returns a tuple ``(picks_list, status)`` where status is one of
    "detected", "no_data", or "failed".
    """
    net = station_cfg["network"]
    sta = station_cfg["station"]

    channel_pattern, is_3c, fallback_pattern = get_detection_channels(station_cfg)

    # Find processed files
    files = find_processed_files(station_cfg, config["input_dir"], year, jday, channel_pattern)

    # Try fallback channel pattern if primary found nothing
    if not files and fallback_pattern:
        logger.info(
            "%s.%s %s/%s: no %s files, falling back to %s",
            net, sta, year, jday, channel_pattern, fallback_pattern,
        )
        files = find_processed_files(station_cfg, config["input_dir"], year, jday, fallback_pattern)
        is_3c = False

    if not files:
        logger.debug("No processed files for %s.%s %s/%s (pattern=%s)", net, sta, year, jday, channel_pattern)
        return [], "no_data"

    # 3C graceful degradation
    if is_3c and len(files) < 3:
        logger.warning(
            "%s.%s %s/%s: expected 3C but found %d file(s) -- running with available data",
            net, sta, year, jday, len(files),
        )

    # Load waveforms
    stream = load_waveforms(files, logger)
    if len(stream) == 0:
        logger.warning("Empty stream for %s.%s %s/%s after loading", net, sta, year, jday)
        return [], "detected"

    logger.info(
        "Detecting %s.%s %s/%s -- %d trace(s), %d file(s)",
        net, sta, year, jday, len(stream), len(files),
    )

    # Apply highpass pre-filter before PhaseNet
    hp_freq = config.get("highpass_freq")
    if hp_freq:
        stream.filter("highpass", freq=hp_freq, corners=4, zerophase=True)
        logger.debug("%s.%s %s/%s: applied %.1f Hz highpass pre-filter",
                     net, sta, year, jday, hp_freq)

    # Run detection and measure amplitudes
    try:
        picks_list = extract_picks(model, stream, config, logger)
        measure_amplitudes(picks_list, stream, config, logger)
        logger.info("%s.%s %s/%s -- %d pick(s)", net, sta, year, jday, len(picks_list))
        return picks_list, "detected"
    except RuntimeError as exc:
        logger.error("Detection runtime error for %s.%s %s/%s: %s: %s",
                      net, sta, year, jday, type(exc).__name__, exc)
        return [], "failed"
    except Exception as exc:
        logger.error("Detection failed for %s.%s %s/%s: %s: %s",
                      net, sta, year, jday, type(exc).__name__, exc)
        return [], "failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    config = load_config(args.config, defaults=DEFAULTS)
    stations = load_stations(config["stations_file"])
    logger = setup_logging("detect", config, debug=args.debug)
    metrics = MetricsWriter(Path(config["log_dir"]) / "metrics.jsonl")

    logger.info("=" * 60)
    logger.info("Phase detection & picking -- %d station(s)", len(stations))
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
        end_time = UTCDateTime() - config["detect_latency_hours"] * 3600
        start_time = end_time - config["detect_window_hours"] * 3600
        logger.info("Scheduled mode: %s -> %s", start_time, end_time)

    force = args.force
    if force:
        logger.info("Force mode enabled -- redetecting all days")

    # Load model once
    model = load_phasenet_model(config, logger)

    # Iterate: days (outer) x stations (inner)
    totals: dict[str, int] = {"days": 0, "skipped": 0, "picks": 0, "failed_stations": 0}

    day = UTCDateTime(start_time.year, start_time.month, start_time.day)
    # Subtract 1 second so midnight end times stay on the previous day
    end_adj = end_time - 1
    end_day = UTCDateTime(end_adj.year, end_adj.month, end_adj.day)

    while day <= end_day:
        year = str(day.year)
        jday = f"{day.julday:03d}"
        daily_csv = get_daily_picks_path(config["output_dir"], year, jday)

        # Skip check at the day level
        if daily_csv.exists() and not force:
            logger.debug("Already detected, skipping day %s/%s", year, jday)
            totals["skipped"] += 1
            day += 86400
            continue

        logger.info("=== Day %s/%s ===", year, jday)
        day_picks: list[dict] = []
        day_t0 = time.monotonic()

        for station_cfg in stations:
            net = station_cfg["network"]
            sta = station_cfg["station"]

            try:
                picks, status = detect_station(
                    station_cfg, year, jday, model, config, logger,
                )
                day_picks.extend(picks)
                if status == "failed":
                    totals["failed_stations"] += 1
            except Exception as exc:
                logger.error(
                    "Unexpected error detecting %s.%s %s/%s: %s: %s",
                    net, sta, year, jday, type(exc).__name__, exc,
                )
                totals["failed_stations"] += 1

        # Write single daily CSV (header-only if no picks)
        try:
            write_picks_csv(day_picks, daily_csv, logger)
            totals["days"] += 1
            totals["picks"] += len(day_picks)
        except OSError as exc:
            logger.error("Cannot write daily picks for %s/%s: %s", year, jday, exc)

        day_elapsed = time.monotonic() - day_t0
        metrics.record(
            "detect",
            day=f"{year}/{jday}",
            picks=len(day_picks),
            duration_s=round(day_elapsed, 1),
        )

        # Explicit cleanup after each day
        gc.collect()

        day += 86400

    # Summary
    logger.info("=" * 60)
    logger.info(
        "Done. days=%d  skipped=%d  picks=%d  failed_stations=%d",
        totals["days"], totals["skipped"], totals["picks"], totals["failed_stations"],
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
