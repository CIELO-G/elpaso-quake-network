#!/usr/bin/env python3
"""
detect.py - Seismic phase detection and picking (standalone PhaseNet)

Step 3/5 of the El Paso seismic processing pipeline.
Uses the standalone PhaseNet (TensorFlow-based) to pick P- and S-wave
arrivals and measure amplitudes on raw waveforms.

Usage (run from project root):
    python 3-detection/detect.py --config 3-detection/config.yaml
    python 3-detection/detect.py --config 3-detection/config.yaml --start 2026-01-29 --end 2026-01-29
    python 3-detection/detect.py --config 3-detection/config.yaml --force --debug
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
from obspy import Stream, read

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.atomic_io import atomic_write_rows
from lib.config import load_config, load_stations
from lib.logger import MetricsWriter, setup_logging
from lib.models import channel_layout, velocity_sensor
from lib.pipeline_stage import iter_days, resolve_time_window
from lib.waveform_qc import mask_impossible_samples

DEFAULTS = {
    "stations_file": "stations.json",
    "input_dir": "output",
    "output_dir": "output",
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
    "phasenet_model_dir": "3-detection/model/190703-214543",
    "phasenet_python": None,  # Python with TF; None = same interpreter
    "p_threshold": 0.4,
    "s_threshold": 0.4,
    "highpass_freq": 3.0,
    "min_peak_distance": 50,
    # B6: stations per PhaseNet input file. PhaseNet holds one whole input
    # file (all its stations x the full day x 3 components, float32, plus
    # the U-Net activations) in memory at once, so memory grows with the
    # number of stations per file, not with the network. 6 stations/day is
    # ~15 GB peak RSS (measured: 13 stations in one file 25.6 GB, groups of 4
    # 12.4 GB, of 8 17.7 GB); picks identical to a single file (STATUS 2026-10-12).
    "phasenet_station_group_size": 6,
    "detect_window_hours": 24,
    "detect_latency_hours": 6,
}

CSV_COLUMNS = [
    "network",
    "station",
    "location",
    "channel",
    "phase",
    "time",
    "probability",
    "model",
    "amplitude",
    "amplitude_channel",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect and pick seismic phases using standalone PhaseNet",
    )
    parser.add_argument("--config", required=True, help="Path to YAML configuration file")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument(
        "--force", action="store_true", help="Redetect even if output CSV already exists"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# PhaseNet station format conversion
# ---------------------------------------------------------------------------


def build_phasenet_stations(stations: list[dict]) -> dict:
    """Convert our stations.json to PhaseNet's station dict format.

    PhaseNet expects::

        {"NET.STA.LOC.CHAN_PREFIX": {
            "latitude": ..., "longitude": ..., "elevation(m)": ...,
            "unit": "m/s", "component": ["E","N","Z"],
            "response": [sens, sens, sens]
        }}
    """
    pn_stations: dict = {}
    for s in stations:
        net = s["network"]
        sta = s["station"]
        loc = s.get("location", "00")
        # Both helpers raise StationValidationError naming the station; the
        # same checks run at load time, so this only trips on a stale list.
        _kind, sensor = velocity_sensor(s)
        chan_prefix, components = channel_layout(s)
        response = [sensor["sensitivity"]] * len(components)

        station_id = f"{net}.{sta}.{loc}.{chan_prefix}"
        pn_stations[station_id] = {
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "elevation(m)": s["elevation_m"],
            "unit": "m/s",
            "component": components,
            "response": response,
        }

    return pn_stations


# ---------------------------------------------------------------------------
# Raw data handling
# ---------------------------------------------------------------------------


def find_raw_files(input_dir: str, year: str, jday: str) -> list[Path]:
    """Find all raw mseed files for a given day."""
    raw_dir = Path(input_dir) / "1-raw" / year / jday
    if not raw_dir.is_dir():
        return []
    return sorted(raw_dir.glob("*.mseed"))


def group_raw_files(raw_files: list[Path], group_size: int) -> list[list[Path]]:
    """Split a day's raw files into station groups of at most ``group_size``.

    Files are grouped by NET.STA (sorted), so one station's channels always
    land in the same PhaseNet input file. PhaseNet reads each input file as
    one array and drops stations with no traces in that file, so grouping
    bounds peak memory without changing the picks.
    """
    by_station: dict[str, list[Path]] = {}
    for f in raw_files:
        parts = f.name.split(".")
        key = ".".join(parts[:2]) if len(parts) >= 2 else f.name
        by_station.setdefault(key, []).append(f)
    stations = sorted(by_station)
    size = max(1, int(group_size)) if group_size else len(stations) or 1
    return [
        [f for sta in stations[i:i + size] for f in by_station[sta]]
        for i in range(0, len(stations), size)
    ]


def day_time_span(raw_files: list[Path], logger) -> tuple | None:
    """(earliest start, latest end) over all of a day's raw files, from headers."""
    t0 = t1 = None
    for fp in raw_files:
        try:
            for tr in read(str(fp), headonly=True):
                t0 = tr.stats.starttime if t0 is None else min(t0, tr.stats.starttime)
                t1 = tr.stats.endtime if t1 is None else max(t1, tr.stats.endtime)
        except Exception as exc:
            logger.warning("Cannot read header of %s: %s: %s", fp, type(exc).__name__, exc)
    return None if t0 is None else (t0, t1)


def merge_raw_data(raw_files: list[Path], output_path: Path, logger, span=None) -> bool:
    """Read raw mseed files and write one merged file for PhaseNet.

    ``span`` = (start, end) pads/trims every trace to the same window. With
    station groups (B6) each group file must cover the whole day's span:
    PhaseNet starts its 30 s windows at a file's earliest sample, so files
    with different starts would cut the windows at different places and
    shift picks at window edges. Padding is zeros, as PhaseNet's own reader
    does for a station that starts late.
    """
    st = Stream()
    for fp in raw_files:
        try:
            st += read(str(fp))
        except Exception as exc:
            logger.warning("Cannot read %s: %s: %s", fp, type(exc).__name__, exc)
    if len(st) == 0:
        return False
    # Corrupt-record guard: impossible counts become gaps before interpolation
    mask_impossible_samples(st, logger=logger)
    try:
        st.merge(method=1, fill_value="interpolate")
    except Exception as exc:
        logger.warning("Merge warning: %s", exc)
    if span is not None:
        st.trim(span[0], span[1], pad=True, fill_value=0)
    st.write(str(output_path), format="MSEED")
    return True


# ---------------------------------------------------------------------------
# PhaseNet subprocess
# ---------------------------------------------------------------------------


def _resolve_python(config: dict, logger) -> list[str]:
    """Build the command prefix for running PhaseNet.

    ``phasenet_python`` in config can be:
      - null / omitted  → use the current interpreter
      - an absolute path → use that Python binary directly
      - a bare name      → a conda env name; resolved to that env's python
        binary directly (sibling of this interpreter's env), falling back
        to ``conda run -n``.

    The direct-binary resolution matters: when the pipeline is launched by
    the desktop app (Finder), PATH is minimal and bare ``conda`` does not
    exist — that broke detection for 2026-07-08..10.
    """
    val = config.get("phasenet_python")
    if not val:
        return [sys.executable]
    if os.path.sep in val or os.path.isabs(val):
        return [val]

    # Conda env name. Prefer the env's python directly — no conda, no PATH
    # dependence, no `conda run` startup overhead. sys.executable is
    # <conda_root>/envs/<this_env>/bin/python, so siblings live two up.
    from pathlib import Path as _Path

    env_python = _Path(sys.executable).parents[2] / val / "bin" / "python"
    if env_python.exists():
        logger.info("Using conda env '%s' for PhaseNet (%s)", val, env_python)
        return [str(env_python)]

    import shutil as _shutil

    conda = _shutil.which("conda") or os.environ.get("CONDA_EXE")
    if conda:
        logger.info("Using conda env '%s' for PhaseNet (via %s)", val, conda)
        return [conda, "run", "-n", val, "python"]
    raise RuntimeError(
        f"Cannot resolve conda env '{val}': {env_python} does not exist and "
        "'conda' is not on PATH. Set phasenet_python to an absolute "
        "interpreter path in 3-detection/config.yaml."
    )


def run_phasenet(config: dict, tmp_dir: str, logger) -> bool:
    """Call PhaseNet predict.py via subprocess."""
    predict_py = str(Path(__file__).resolve().parent / "phasenet" / "predict.py")
    model_dir = config["phasenet_model_dir"]
    python_cmd = _resolve_python(config, logger)

    cmd = [
        *python_cmd,
        predict_py,
        "--model_dir",
        model_dir,
        "--data_list",
        os.path.join(tmp_dir, "fnames.csv"),
        "--data_dir",
        tmp_dir,
        "--stations",
        os.path.join(tmp_dir, "phasenet_stations.json"),
        "--format",
        "mseed_array",
        "--amplitude",
        "--highpass_filter",
        str(config["highpass_freq"]),
        "--min_p_prob",
        str(config["p_threshold"]),
        "--min_s_prob",
        str(config["s_threshold"]),
        "--mpd",
        str(config.get("min_peak_distance", 50)),
        "--result_dir",
        os.path.join(tmp_dir, "results"),
        "--result_fname",
        "picks",
    ]

    logger.info("Running PhaseNet: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        logger.error("PhaseNet timed out after 3600s")
        return False

    if result.returncode != 0:
        logger.error(
            "PhaseNet failed (rc=%d):\nstdout: %s\nstderr: %s",
            result.returncode,
            result.stdout[-2000:],
            result.stderr[-2000:],
        )
        return False

    logger.info("PhaseNet: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.debug("PhaseNet stderr (last 1000 chars): %s", result.stderr.strip()[-1000:])

    return True


# ---------------------------------------------------------------------------
# Parse PhaseNet output
# ---------------------------------------------------------------------------


def parse_phasenet_picks(picks_csv: str, logger) -> list[dict]:
    """Parse PhaseNet output CSV and map to our pick format.

    PhaseNet columns:
        station_id, begin_time, phase_index, phase_time, phase_score,
        phase_type, file_name [, phase_amplitude, phase_amp]

    Our columns:
        network, station, location, channel, phase, time, probability,
        model, amplitude, amplitude_channel
    """
    if not os.path.exists(picks_csv):
        logger.warning("PhaseNet picks file not found: %s", picks_csv)
        return []

    df = pd.read_csv(picks_csv)
    if len(df) == 0:
        return []

    has_amplitude = "phase_amplitude" in df.columns

    results: list[dict] = []
    for _, row in df.iterrows():
        station_id = str(row["station_id"])  # e.g. "AM.R0F2D.00.EH"
        parts = station_id.split(".")
        if len(parts) < 4:
            logger.warning("Unexpected station_id format: %s", station_id)
            continue

        net, sta, loc, chan_prefix = parts[0], parts[1], parts[2], parts[3]

        # Amplitude
        amplitude = ""
        amplitude_channel = ""
        if has_amplitude and pd.notna(row.get("phase_amplitude")):
            try:
                amp_val = float(row["phase_amplitude"])
            except (ValueError, TypeError):
                amp_val = 0.0
            if amp_val != 0 and not math.isnan(amp_val):
                amplitude = f"{amp_val:.6e}"
                amplitude_channel = chan_prefix + "Z"

        results.append(
            {
                "network": net,
                "station": sta,
                "location": loc,
                "channel": chan_prefix + "Z",
                "phase": row["phase_type"],
                "time": row["phase_time"],
                "probability": f"{float(row['phase_score']):.4f}",
                "model": "PhaseNet:standalone",
                "amplitude": amplitude,
                "amplitude_channel": amplitude_channel,
            }
        )

    return results


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


def get_daily_picks_path(output_dir: str, year: str, jday: str) -> Path:
    """Return the CSV path for a day's picks."""
    return Path(output_dir) / "3-picks" / year / jday / f"{year}.{jday}.picks.csv"


def write_picks_csv(picks_list: list[dict], output_path: Path, logger) -> None:
    """Write picks to CSV.  Always writes header even if no picks."""
    # Atomic: this file is the "day done" marker for detection, so a
    # truncated one would make a half-written day look complete.
    atomic_write_rows(output_path, picks_list, CSV_COLUMNS)
    logger.info("Wrote %d pick(s) to %s", len(picks_list), output_path)


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
    logger.info("Phase detection & picking (standalone PhaseNet) -- %d station(s)", len(stations))
    logger.info("=" * 60)

    # Determine time window
    start_time, end_time = resolve_time_window(
        args,
        config,
        window_hours_key="detect_window_hours",
        latency_hours_key="detect_latency_hours",
        logger=logger,
    )

    force = args.force
    if force:
        logger.info("Force mode enabled -- redetecting all days")

    # Build PhaseNet stations dict once (reused for every day)
    pn_stations = build_phasenet_stations(stations)

    # Iterate over days
    totals: dict[str, int] = {"days": 0, "skipped": 0, "picks": 0, "failed": 0}

    for year, jday, day in iter_days(start_time, end_time):
        daily_csv = get_daily_picks_path(config["output_dir"], year, jday)

        # Skip check
        if daily_csv.exists() and not force:
            logger.debug("Already detected, skipping day %s/%s", year, jday)
            totals["skipped"] += 1
            continue

        logger.info("=== Day %s/%s ===", year, jday)
        day_t0 = time.monotonic()

        # --- Step 1: Find raw data ---
        raw_files = find_raw_files(config["input_dir"], year, jday)
        if not raw_files:
            logger.info("No raw data for %s/%s, writing empty CSV", year, jday)
            write_picks_csv([], daily_csv, logger)
            totals["days"] += 1
            continue

        with tempfile.TemporaryDirectory(prefix="phasenet_") as tmp_dir:
            # --- Step 2: Merge raw data, one mseed per station group (B6) ---
            groups = group_raw_files(raw_files, config.get("phasenet_station_group_size", 0))
            span = day_time_span(raw_files, logger) if len(groups) > 1 else None
            merged_files: list[str] = []
            for k, group in enumerate(groups):
                merged_mseed = os.path.join(tmp_dir, f"{year}.{jday}.g{k:02d}.mseed")
                if merge_raw_data(group, merged_mseed, logger, span=span):
                    merged_files.append(merged_mseed)
            if not merged_files:
                logger.warning("No valid data for %s/%s after merge", year, jday)
                write_picks_csv([], daily_csv, logger)
                totals["days"] += 1
                continue

            logger.info(
                "Merged %d raw file(s) into %d station group(s) of <= %s stations",
                len(raw_files), len(merged_files), config.get("phasenet_station_group_size", "all"),
            )

            # --- Step 3: Generate PhaseNet input files ---
            fnames_csv = os.path.join(tmp_dir, "fnames.csv")
            with open(fnames_csv, "w") as f:
                f.write("fname\n")
                for merged_mseed in merged_files:
                    f.write(os.path.basename(merged_mseed) + "\n")

            stations_json = os.path.join(tmp_dir, "phasenet_stations.json")
            with open(stations_json, "w") as f:
                json.dump(pn_stations, f, indent=2)

            results_dir = os.path.join(tmp_dir, "results")
            os.makedirs(results_dir, exist_ok=True)

            # --- Step 4: Run PhaseNet ---
            if not run_phasenet(config, tmp_dir, logger):
                # PhaseNet failures are transient (OOM, GPU error, killed
                # subprocess). DO NOT write an empty picks CSV here — that
                # would mark the day as "done" and prevent retry forever.
                # Leave daily_csv absent; record the failure; orchestrator
                # exit code will be non-zero so retries kick in.
                logger.error(
                    "PhaseNet failed for %s/%s — leaving day unmarked for retry", year, jday
                )
                totals["failed"] += 1
                continue

            # --- Step 5: Parse picks and write daily CSV ---
            picks_csv = os.path.join(results_dir, "picks.csv")
            day_picks = parse_phasenet_picks(picks_csv, logger)

        write_picks_csv(day_picks, daily_csv, logger)
        totals["days"] += 1
        totals["picks"] += len(day_picks)

        day_elapsed = time.monotonic() - day_t0
        metrics.record(
            "detect",
            day=f"{year}/{jday}",
            picks=len(day_picks),
            duration_s=round(day_elapsed, 1),
        )
        logger.info("Day %s/%s: %d picks in %.1fs", year, jday, len(day_picks), day_elapsed)

        gc.collect()

    # Summary
    logger.info("=" * 60)
    logger.info(
        "Done. days=%d  skipped=%d  picks=%d  failed=%d",
        totals["days"],
        totals["skipped"],
        totals["picks"],
        totals["failed"],
    )
    logger.info("=" * 60)

    # Non-zero exit on PhaseNet failure so the orchestrator retries (was
    # writing empty CSV and marking day "done" forever).
    if totals["failed"] > 0:
        logger.error(
            "%d day(s) had PhaseNet failures and were left unmarked. Re-run will retry them.",
            totals["failed"],
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
