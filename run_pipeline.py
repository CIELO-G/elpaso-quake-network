#!/usr/bin/env python
"""
Pipeline orchestrator — runs the seismic processing steps (STEPS) as subprocesses
and writes status to output/pipeline_status.json for the dashboard to read.

Supports three modes:
  Single-run:  python run_pipeline.py --start 2026-01-01 --end 2026-01-01
  Continuous:  python run_pipeline.py --continuous --start 2025-11-01
  Gap-fill:    python run_pipeline.py --gap-fill [--start DATE] [--end DATE]
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # allow `from lib.constants import ...`

from lib.monitoring import notify, send_email, stations_without_data  # noqa: E402
from lib.config import load_stations
from lib.pipeline_state import PipelineState, load_state, save_state  # noqa: E402
from lib.constants import (  # noqa: E402  (path setup above)
    CONTINUOUS_LAG_HOURS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_WAIT_SECONDS,
    PIPELINE_START_DATE_ISO,
)

OUTPUT_DIR = ROOT / "output"
STATUS_FILE = ROOT / "output" / "pipeline_status.json"
EVENTS_DIR = ROOT / "output" / "4-events"
STATIONS_FILE = ROOT / "stations.json"
RAW_DIR = ROOT / "output" / "1-raw"
PROCESSED_DIR = ROOT / "output" / "2-processed"

# ── continuous-mode defaults ──────────────────────────────────────────────────

LAG_HOURS = CONTINUOUS_LAG_HOURS
MAX_RETRIES = DEFAULT_MAX_RETRIES
RETRY_WAIT = DEFAULT_RETRY_WAIT_SECONDS
PIPELINE_START = date.fromisoformat(PIPELINE_START_DATE_ISO)
MAX_SKIP_RETRY_PASSES = 3  # retry all skipped days this many times after catching up
SKIP_RETRY_WAIT = 600  # 10-minute cooldown between retry passes
# Ingest exit code meaning "some data stored, some chunks failed" (see
# 1-ingestion/ingest.py exit_code_for). The day proceeds but is recorded as
# partial and re-ingested on later caught-up cycles, up to this age.
INGEST_EXIT_PARTIAL = 2
PARTIAL_MAX_AGE_DAYS = 7

# ── graceful shutdown ────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_sigterm(signum, frame):
    """Convert SIGTERM into the same flow as KeyboardInterrupt."""
    global _shutdown_requested
    _shutdown_requested = True
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _handle_sigterm)

# ── email alert config (from environment variables) ──────────────────────────
#   ALERT_EMAIL_TO     — recipient address (required for alerts)
#   ALERT_EMAIL_FROM   — sender address (defaults to ALERT_EMAIL_TO)
#   ALERT_SMTP_HOST    — SMTP server (default: smtp.gmail.com)
#   ALERT_SMTP_PORT    — SMTP port (default: 587)
#   ALERT_SMTP_PASSWORD — app password or SMTP password

# ── step registry ────────────────────────────────────────────────────────────
# Order is execution order. Code selects steps by name; the 1-based number
# shown in status and taken by --start-step/--end-step is the position here.
# An "optional" step runs only while its config says `enabled: true` (read
# before every run, no restart needed), and its failure is alerted but does
# not fail the day.

STEPS: list[dict[str, Any]] = [
    {
        "name": "ingest",
        "script": "1-ingestion/ingest.py",
        "config": "1-ingestion/config.yaml",
        "log_file": "logs/ingest.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        "name": "process",
        "script": "2-processing/process.py",
        "config": "2-processing/config.yaml",
        "log_file": "logs/process.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        "name": "detect",
        "script": "3-detection/detect.py",
        "config": "3-detection/config.yaml",
        "log_file": "logs/detect.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        "name": "associate",
        "script": "4-association/associate.py",
        "config": "4-association/config.yaml",
        "log_file": "logs/associate.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        # Second detector: adds the events PhaseNet missed (lib/detector_merge.py).
        "name": "phaseno",
        "script": "3-detection/phaseno/step.py",
        "config": "3-detection/phaseno/config.yaml",
        "log_file": "logs/phaseno.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
        "optional": True,
    },
    {
        "name": "catalog",
        "script": "5-catalog/catalog.py",
        "config": "5-catalog/config.yaml",
        "log_file": "logs/catalog.log",
        "accepts_dates": False,
        "accepts_force": False,
        "accepts_rebuild": True,
    },
]
STEP_NAMES = [s["name"] for s in STEPS]


def _steps_after(name: str) -> set[str]:
    """Names of the steps that run after `name`."""
    return set(STEP_NAMES[STEP_NAMES.index(name) + 1 :])


def _step_enabled(step: dict) -> bool:
    """Required steps always run; an optional one only with `enabled: true` in its config."""
    if not step.get("optional"):
        return True
    try:
        cfg = yaml.safe_load((ROOT / step["config"]).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"  WARNING: cannot read {step['config']} ({exc}) — {step['name']} skipped")
        return False
    return cfg.get("enabled") is True


def _alert_optional_failure(step: dict, day_str: str, returncode: int) -> None:
    end = (date.fromisoformat(day_str) + timedelta(days=1)).isoformat()
    _notify(
        f"[El Paso Pipeline] optional step {step['name']} failed for {day_str}",
        f"{step['name']} exited {returncode} for {day_str}; see {step['log_file']}.\n"
        f"The day completed without it. To redo it by hand:\n"
        f"  python {step['script']} --config {step['config']} --start {day_str} --end {end}\n"
        f"  python 5-catalog/catalog.py --config 5-catalog/config.yaml\n",
    )

# ── status helpers ───────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(data: dict) -> None:
    """Atomically write status JSON (write tmp then rename)."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATUS_FILE)


def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _target_date() -> date:
    """Latest date eligible for processing (now minus lag)."""
    return (datetime.now(timezone.utc) - timedelta(hours=LAG_HOURS)).date()


def _make_step_statuses(initial: str = "pending") -> list[dict]:
    return [
        {
            "number": n,
            "name": s["name"],
            # A disabled optional step will be skipped: say so from the start.
            "status": initial if _step_enabled(s) else "skipped",
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "log_file": s["log_file"],
        }
        for n, s in enumerate(STEPS, 1)
    ]


def _send_alert(subject: str, body: str) -> None:
    """Email alert (see lib.monitoring.send_email). Kept for existing call sites."""
    send_email(subject, body)


# ── gap-fill helpers ─────────────────────────────────────────────────────────


def _load_station_expectations() -> list[tuple[str, str, date, date | None]]:
    """Read stations.json and return (network, station, start_date, end_date) tuples.

    ``end_date`` is None for an active station; a retired one is only
    expected through its end_date (inclusive).
    """
    stations = load_stations(STATIONS_FILE)  # validated: dates, instrument, channels
    out = []
    for s in stations:
        end = s.get("end_date") or None
        out.append((
            s["network"], s["station"],
            date.fromisoformat(s["start_date"]),
            date.fromisoformat(end) if end else None,
        ))
    return out


def _expected_on(stations, day: date) -> set[str]:
    """NET.STA ids expected to have data on ``day``; tolerates 3- or 4-tuples."""
    ids = set()
    for entry in stations:
        net, sta, start = entry[0], entry[1], entry[2]
        end = entry[3] if len(entry) > 3 else None
        if start <= day and (end is None or day <= end):
            ids.add(f"{net}.{sta}")
    return ids


def _scan_gaps(
    scan_start: date,
    scan_end: date,
    stations: list[tuple],
) -> list[tuple[date, list[str]]]:
    """Find days with station data missing from BOTH 1-raw/ and 2-processed/.

    Local raw is pruned to the newest few days after archiving
    (scripts/prune_raw.py), so a missing raw day-dir does NOT mean the
    day was never ingested — processed output proves it was. Without the
    processed check, --gap-fill after a prune would try to re-download
    and force-reprocess the entire history.

    Returns list of (day, [missing_station_ids]) for days that have gaps.
    """
    gaps: list[tuple[date, list[str]]] = []
    day = scan_start
    while day <= scan_end:
        doy = day.timetuple().tm_yday
        rel = f"{day.year}/{doy:03d}"

        # stations expected to be online this day (start_date <= day <= end_date)
        expected = _expected_on(stations, day)
        if not expected:
            day += timedelta(days=1)
            continue

        # parse NET.STA from mseed filenames in either tree
        present = set()
        for base in (RAW_DIR, PROCESSED_DIR):
            day_dir = base / rel
            if not day_dir.exists():
                continue
            for f in day_dir.iterdir():
                parts = f.name.split(".")
                if len(parts) >= 2:
                    present.add(f"{parts[0]}.{parts[1]}")

        missing = expected - present
        if not missing and present and not _events_file(day).exists():
            # Ingested and processed, but detect/associate never finished
            # (crash mid-day). Re-running the day with --force completes it.
            missing = {"(pipeline incomplete: no events file)"}
        if missing:
            gaps.append((day, sorted(missing)))

        day += timedelta(days=1)
    return gaps


# ── main ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the seismic processing pipeline.")
    p.add_argument("--start", help="Start date (forwarded to the per-day steps)")
    p.add_argument("--end", help="End date (forwarded to the per-day steps)")
    p.add_argument(
        "--force",
        action="store_true",
        help="Force reprocessing (--force for the per-day steps, --rebuild for catalog)",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging in all steps")
    p.add_argument(
        "--start-step", type=int, default=1, metavar="N", help="First step to run (default: 1; numbers follow STEPS order)"
    )
    p.add_argument(
        "--end-step", type=int, default=len(STEPS), metavar="N",
        help=f"Last step to run (default: {len(STEPS)}, all)"
    )
    p.add_argument(
        "--continuous",
        action="store_true",
        help="Run continuously — process one day at a time, "
        "then wait for new data (Ctrl+C to stop)",
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="Validate environment (FDSNWS, disk, GPU, config) and exit",
    )
    p.add_argument(
        "--gap-fill",
        action="store_true",
        help="Scan for days with missing station data and re-run the pipeline to fill gaps",
    )
    p.add_argument(
        "--archive-only",
        action="store_true",
        help="With --gap-fill: only ingest + process the gap days (new-station backfill); "
             "never re-detect or re-associate days that already have events",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands that would be run, without executing them",
    )
    return p.parse_args()


def build_command(
    step: dict,
    args: argparse.Namespace,
    *,
    start_override: str | None = None,
    end_override: str | None = None,
    force_override: bool | None = None,
) -> list[str]:
    """Build the subprocess command list for a step."""
    force = args.force if force_override is None else force_override
    # -u: stage stdout goes to logs/pipeline_stdout.log; unbuffered so the
    # file reflects what has happened, not what fit in an 8 KB buffer.
    cmd = [sys.executable, "-u", str(ROOT / step["script"]), "--config", str(ROOT / step["config"])]
    if step["accepts_dates"]:
        start = start_override or args.start
        end = end_override or args.end
        if start and end and start == end:
            # Same date means "process this whole day", so end = next day
            end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        if start:
            cmd += ["--start", start]
        if end:
            cmd += ["--end", end]
    if force:
        if step["accepts_force"]:
            cmd.append("--force")
        elif step["accepts_rebuild"]:
            cmd.append("--rebuild")
    if args.debug:
        cmd.append("--debug")
    return cmd


# ── single-run mode (original behaviour) ─────────────────────────────────────


def single_run(args: argparse.Namespace) -> None:
    if args.start_step < 1 or args.end_step > len(STEPS) or args.start_step > args.end_step:
        print(f"Error: invalid step range {args.start_step}-{args.end_step}", file=sys.stderr)
        sys.exit(1)

    # ── build initial status ─────────────────────────────────────────────
    pipeline_started = _now()
    step_statuses = []
    for n, step in enumerate(STEPS, 1):
        if n < args.start_step or n > args.end_step or not _step_enabled(step):
            status = "skipped"
        else:
            status = "pending"
        step_statuses.append(
            {
                "number": n,
                "name": step["name"],
                "status": status,
                "started_at": None,
                "finished_at": None,
                "return_code": None,
                "log_file": step["log_file"],
            }
        )

    status_data = {
        "pipeline": {
            "status": "running",
            "started_at": pipeline_started,
            "finished_at": None,
            "args": {
                "start": args.start,
                "end": args.end,
                "force": args.force,
                "debug": args.debug,
                "start_step": args.start_step,
                "end_step": args.end_step,
            },
        },
        "steps": step_statuses,
    }
    _write_status(status_data)

    # ── run steps ────────────────────────────────────────────────────────
    total = args.end_step - args.start_step + 1
    failed = False

    for n, step in enumerate(STEPS, 1):
        if step_statuses[n - 1]["status"] == "skipped":
            continue

        idx = n - args.start_step + 1
        print(f"[{idx}/{total}] Running {step['name']} ...")

        # mark running
        step_statuses[n - 1]["status"] = "running"
        step_statuses[n - 1]["started_at"] = _now()
        _write_status(status_data)

        # execute
        cmd = build_command(step, args)

        if getattr(args, "dry_run", False):
            print(f"  [dry-run] {' '.join(cmd)}")
            step_statuses[n - 1]["status"] = "completed"
            step_statuses[n - 1]["finished_at"] = _now()
            step_statuses[n - 1]["return_code"] = 0
            _write_status(status_data)
            continue

        t0 = datetime.now(timezone.utc)
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        t1 = datetime.now(timezone.utc)
        elapsed = (t1 - t0).total_seconds()

        # record result
        step_statuses[n - 1]["finished_at"] = _now()
        step_statuses[n - 1]["return_code"] = result.returncode

        if result.returncode == 0:
            step_statuses[n - 1]["status"] = "completed"
            print(f"[{idx}/{total}] Completed in {_format_elapsed(elapsed)}")
        elif step.get("optional"):
            step_statuses[n - 1]["status"] = "failed"
            print(f"[{idx}/{total}] FAILED (exit code {result.returncode}) — optional, continuing")
        else:
            step_statuses[n - 1]["status"] = "failed"
            print(f"[{idx}/{total}] FAILED (exit code {result.returncode})")
            status_data["pipeline"]["status"] = "failed"
            status_data["pipeline"]["finished_at"] = _now()
            _write_status(status_data)
            failed = True
            break

        _write_status(status_data)

    if not failed:
        status_data["pipeline"]["status"] = "completed"
        status_data["pipeline"]["finished_at"] = _now()
        _write_status(status_data)

    # ── summary ──────────────────────────────────────────────────────────
    print()
    print(f"Pipeline {status_data['pipeline']['status']}.")


# ── continuous mode ──────────────────────────────────────────────────────────


def _events_file(day: date) -> Path:
    doy = day.timetuple().tm_yday
    return EVENTS_DIR / f"{day.year}" / f"{doy:03d}" / f"{day.year}.{doy:03d}.events.csv"


def _iter_day_dirs(base: Path):
    """Yield dates for every YYYY/DOY directory under ``base``."""
    if not base.exists():
        return
    for year_dir in base.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        for doy_dir in year_dir.iterdir():
            if not doy_dir.is_dir() or not doy_dir.name.isdigit():
                continue
            yield date(year, 1, 1) + timedelta(days=int(doy_dir.name) - 1)


def _find_next_day() -> date:
    """Auto-detect where to resume: the day after the last COMPLETED day.

    A day counts as completed only if it has both a 2-processed/ directory
    and its 4-events/ events file. Processed alone is not enough: process.py
    creates that directory before detect/associate/catalog have run, so a
    crash mid-day used to make the restart skip that day forever. Events
    alone is not enough either: traces crossing midnight create spill-over
    directories in raw/picks/events (never in processed) for the next day.
    """
    latest = None
    for d in _iter_day_dirs(PROCESSED_DIR):
        if _events_file(d).exists() and (latest is None or d > latest):
            latest = d
    if latest is None:
        return PIPELINE_START
    return latest + timedelta(days=1)


@dataclass
class DayResult:
    """Outcome of running the five steps for one day."""

    ok: bool
    partial: bool
    timing: dict


def _run_day(
    day_str: str,
    args: argparse.Namespace,
    status_data: dict,
    *,
    force: bool | None = None,
    only_steps: set[str] | None = None,
) -> DayResult:
    """Run the pipeline steps for a single day (all, or the names in `only_steps`).

    Updates ``status_data["steps"]`` in-place so the dashboard sees
    real-time step progress. ``force`` overrides ``args.force`` (used when
    re-running a partial day so downstream steps reprocess the new data).
    Ingest exit code ``INGEST_EXIT_PARTIAL`` lets the day continue but marks
    the result ``partial``.
    """
    steps = status_data["steps"]
    timing: dict = {"day": day_str, "total": 0.0, "steps": {}}
    partial = False

    for i, step in enumerate(STEPS):
        if (only_steps is not None and step["name"] not in only_steps) or not _step_enabled(step):
            steps[i]["status"] = "skipped"
            continue
        steps[i]["status"] = "running"
        steps[i]["started_at"] = _now()
        steps[i]["finished_at"] = None
        steps[i]["return_code"] = None
        _write_status(status_data)

        end_str = (date.fromisoformat(day_str) + timedelta(days=1)).isoformat()
        cmd = build_command(
            step, args, start_override=day_str, end_override=end_str, force_override=force
        )
        t0 = datetime.now(timezone.utc)
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

        timing["steps"][step["name"]] = round(elapsed, 1)

        steps[i]["finished_at"] = _now()
        steps[i]["return_code"] = result.returncode

        if result.returncode == 0:
            steps[i]["status"] = "completed"
            print(f"  {step['name']}: completed ({_format_elapsed(elapsed)})")
        elif step["name"] == "ingest" and result.returncode == INGEST_EXIT_PARTIAL:
            steps[i]["status"] = "completed"
            steps[i]["partial"] = True
            partial = True
            print(f"  {step['name']}: completed with gaps ({_format_elapsed(elapsed)}) — day marked partial")
        elif step.get("optional"):
            steps[i]["status"] = "failed"
            print(f"  {step['name']}: FAILED (exit {result.returncode}) — optional, day continues")
            _alert_optional_failure(step, day_str, result.returncode)
        else:
            steps[i]["status"] = "failed"
            print(f"  {step['name']}: FAILED (exit {result.returncode})")
            timing["total"] = round(sum(timing["steps"].values()), 1)
            _write_status(status_data)
            return DayResult(False, partial, timing)

        _write_status(status_data)

    timing["total"] = round(sum(timing["steps"].values()), 1)
    return DayResult(True, partial, timing)


def _notify(subject: str, body: str) -> None:
    """Email + webhook, best effort (prints when neither is configured)."""
    try:
        notify(subject, body)
    except Exception as exc:  # alerting must never take the pipeline down
        print(f"  WARNING: alert delivery failed: {exc}")


STATION_DARK_DAYS = 3
INGEST_SUMMARY = ROOT / "logs" / "ingest_summary.json"


def _ingest_improved_count() -> int:
    """``improved`` from the last ingest run's summary (0 if unreadable)."""
    try:
        return int(json.loads(INGEST_SUMMARY.read_text())["totals"].get("improved", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _check_station_coverage(state: PipelineState, last_day: date) -> None:
    """After each completed day: alert once when a station goes dark for
    STATION_DARK_DAYS consecutive days, and once when it comes back.
    """
    try:
        stations = _load_station_expectations()
        dark = stations_without_data(RAW_DIR, stations, last_day, STATION_DARK_DAYS)
    except Exception as exc:
        print(f"  WARNING: station coverage check failed: {exc}")
        return
    newly_dark = sorted(set(dark) - set(state.dark_stations))
    recovered = sorted(set(state.dark_stations) - set(dark))
    if newly_dark:
        _notify(
            f"[El Paso Pipeline] {len(newly_dark)} station(s) dark for {STATION_DARK_DAYS} days",
            f"No raw data on any of the last {STATION_DARK_DAYS} days (through {last_day}) for: "
            f"{', '.join(newly_dark)}\nCheck the Shake(s) and the FDSN server; the day will "
            f"be re-ingested automatically for up to {PARTIAL_MAX_AGE_DAYS} days.\n",
        )
    if recovered:
        _notify(
            f"[El Paso Pipeline] station(s) back: {', '.join(recovered)}",
            f"Data seen again within the last {STATION_DARK_DAYS} days (through {last_day}).\n",
        )
    if newly_dark or recovered:
        state.dark_stations = sorted(dark)
        save_state(OUTPUT_DIR, state)


def _retry_partial_days(args: argparse.Namespace, status_data: dict, state: PipelineState) -> None:
    """Re-run days whose ingest was partial, with --force so downstream
    steps pick up the late-arriving data. A day leaves the list when ingest
    comes back clean, or when it is older than PARTIAL_MAX_AGE_DAYS (alerted).
    """
    today = _target_date()
    for day_str in list(state.days_partial):
        age = (today - date.fromisoformat(day_str)).days
        if age > PARTIAL_MAX_AGE_DAYS:
            print(f"  {day_str}: still partial after {age} days — giving up")
            state.remove_partial(day_str)
            save_state(OUTPUT_DIR, state)
            # No email: routine for a flaky station; a station that is fully
            # out is reported once by the dark-station alert instead.
            print(f"  {day_str} remains incomplete — retry by hand with "
                  f"`run_pipeline.py --gap-fill --start {day_str} --end {day_str}`")
            continue
        print(f"  Re-ingesting partial day {day_str} (age {age} d) ...")
        status_data["steps"] = _make_step_statuses()
        status_data["pipeline"]["status"] = "running"
        status_data["pipeline"]["args"]["start"] = day_str
        status_data["pipeline"]["args"]["end"] = day_str
        _write_status(status_data)
        # Ingest alone first. Re-processing the day (detect, associate,
        # catalog --rebuild) is only worth it if the re-fetch actually
        # brought in more samples than the tracker already had.
        res = _run_day(day_str, args, status_data, force=True, only_steps={"ingest"})
        if not res.ok:
            print(f"    {day_str} re-ingest failed; will try again next cycle")
            continue
        improved = _ingest_improved_count()
        if improved == 0:
            print(f"    {day_str}: nothing new arrived" + ("" if res.partial else " — now complete"))
            if not res.partial:
                state.remove_partial(day_str)
                save_state(OUTPUT_DIR, state)
            continue
        print(f"    {day_str}: {improved} chunk(s) improved — re-processing")
        res2 = _run_day(day_str, args, status_data, force=True, only_steps=_steps_after("ingest"))
        if res2.ok and not res.partial:
            print(f"    {day_str} is now complete")
            state.remove_partial(day_str)
            save_state(OUTPUT_DIR, state)
        elif res2.ok:
            print(f"    {day_str} re-processed; still partial")
        else:
            print(f"    {day_str} re-processing failed; will try again next cycle")


def continuous_run(args: argparse.Namespace) -> None:
    """Process days one at a time, sleeping when caught up."""
    if args.start:
        current_day = date.fromisoformat(args.start)
    else:
        current_day = _find_next_day()

    days_completed = 0
    state: PipelineState = load_state(OUTPUT_DIR)
    days_skipped: list[str] = list(state.days_skipped)
    pipeline_started = _now()

    print(f"Continuous mode: starting from {current_day}")
    print(f"Lag: {LAG_HOURS}h | Retries: {MAX_RETRIES} | Ctrl+C to stop")
    if days_skipped:
        print(f"Skipped days carried over from the last run: {', '.join(days_skipped)}")
    if state.days_partial:
        print(f"Partial days carried over from the last run: {', '.join(state.days_partial)}")
    print()

    # Persistent status structure shared across iterations
    status_data: dict = {
        "pipeline": {
            "status": "running",
            "mode": "continuous",
            "started_at": pipeline_started,
            "finished_at": None,
            "args": {
                "start": args.start,
                "end": args.end,
                "force": args.force,
                "debug": args.debug,
                "start_step": 1,
                "end_step": len(STEPS),
            },
            "continuous": {
                "current_day": current_day.isoformat(),
                "days_completed": 0,
                "days_skipped": list(days_skipped),
                "days_partial": list(state.days_partial),
                "target_date": _target_date().isoformat(),
                "waiting": False,
                "next_run_at": None,
                "day_times": [],
            },
        },
        "steps": _make_step_statuses(),
    }
    _write_status(status_data)

    cont = status_data["pipeline"]["continuous"]

    try:
        while True:
            target = _target_date()
            cont["target_date"] = target.isoformat()
            cont["current_day"] = current_day.isoformat()

            # ── retry skipped days before sleeping ─────────────────────
            if current_day > target and days_skipped:
                print(f"\nCaught up — retrying {len(days_skipped)} skipped day(s)...")
                still_skipped: list[str] = []

                for pass_num in range(1, MAX_SKIP_RETRY_PASSES + 1):
                    remaining = days_skipped if pass_num == 1 else still_skipped
                    if not remaining:
                        break
                    still_skipped = []
                    print(
                        f"  Retry pass {pass_num}/{MAX_SKIP_RETRY_PASSES} ({len(remaining)} day(s))"
                    )

                    for skip_day in remaining:
                        status_data["steps"] = _make_step_statuses()
                        status_data["pipeline"]["status"] = "running"
                        status_data["pipeline"]["args"]["start"] = skip_day
                        status_data["pipeline"]["args"]["end"] = skip_day
                        _write_status(status_data)

                        print(f"    Retrying {skip_day} ...")
                        res = _run_day(skip_day, args, status_data)
                        ok, timing = res.ok, res.timing

                        if not ok:
                            # one more attempt after a short wait
                            _time.sleep(RETRY_WAIT)
                            status_data["steps"] = _make_step_statuses()
                            _write_status(status_data)
                            res = _run_day(skip_day, args, status_data)
                            ok, timing = res.ok, res.timing

                        if ok:
                            print(f"    {skip_day} recovered!")
                            days_completed += 1
                            cont["days_completed"] = days_completed
                            cont["day_times"].append(timing)
                            cont["day_times"] = cont["day_times"][-100:]
                            state.remove_skipped(skip_day)
                            if res.partial:
                                state.add_partial(skip_day)
                            save_state(OUTPUT_DIR, state)
                        else:
                            print(f"    {skip_day} still failing")
                            still_skipped.append(skip_day)

                    if still_skipped and pass_num < MAX_SKIP_RETRY_PASSES:
                        print(f"  Waiting {SKIP_RETRY_WAIT}s before next pass...")
                        _time.sleep(SKIP_RETRY_WAIT)

                days_skipped = still_skipped
                cont["days_skipped"] = days_skipped
                state.days_skipped = sorted(set(days_skipped))
                save_state(OUTPUT_DIR, state)
                _write_status(status_data)

                if days_skipped:
                    summary = (
                        f"{len(days_skipped)} day(s) remain unrecoverable "
                        f"after {MAX_SKIP_RETRY_PASSES} retry passes: "
                        f"{', '.join(days_skipped)}"
                    )
                    print(f"  {summary}")
                    _send_alert(
                        subject=f"[El Paso Pipeline] {len(days_skipped)} day(s) unrecoverable",
                        body=f"{summary}\n\nThese days may need manual investigation.\n",
                    )
                else:
                    print("  All skipped days recovered!")

            # ── retry partial days (late-arriving data) before sleeping ──
            if current_day > target and state.days_partial:
                _retry_partial_days(args, status_data, state)
                cont["days_partial"] = list(state.days_partial)
                _write_status(status_data)

            # ── caught up — sleep until next day is eligible ──────────
            if current_day > target:
                next_run = datetime(
                    current_day.year,
                    current_day.month,
                    current_day.day,
                    tzinfo=timezone.utc,
                ) + timedelta(hours=LAG_HOURS)
                wait_sec = max(
                    (next_run - datetime.now(timezone.utc)).total_seconds(),
                    60,
                )

                cont["waiting"] = True
                cont["next_run_at"] = next_run.isoformat()
                status_data["pipeline"]["status"] = "waiting"
                # show last-completed steps while sleeping
                _write_status(status_data)

                print(f"Caught up — waiting for {current_day} (~{_format_elapsed(wait_sec)})")

                # sleep in 60-s chunks so Ctrl+C is responsive
                deadline = _time.monotonic() + wait_sec
                while _time.monotonic() < deadline:
                    _time.sleep(min(deadline - _time.monotonic(), 60))

                cont["waiting"] = False
                cont["next_run_at"] = None
                continue

            # ── reset steps for this day ──────────────────────────────
            status_data["steps"] = _make_step_statuses()
            status_data["pipeline"]["status"] = "running"
            status_data["pipeline"]["args"]["start"] = current_day.isoformat()
            status_data["pipeline"]["args"]["end"] = current_day.isoformat()
            _write_status(status_data)

            day_str = current_day.isoformat()
            print(f"[Day {days_completed + 1}] {day_str}  (target: {target})")

            # ── attempt + retries ─────────────────────────────────────
            res = _run_day(day_str, args, status_data)
            success, timing = res.ok, res.timing

            if not success:
                for attempt in range(1, MAX_RETRIES + 1):
                    print(
                        f"  Retry {attempt}/{MAX_RETRIES} for {day_str} (waiting {RETRY_WAIT}s) ..."
                    )
                    _time.sleep(RETRY_WAIT)
                    status_data["steps"] = _make_step_statuses()
                    status_data["pipeline"]["status"] = "running"
                    _write_status(status_data)
                    res = _run_day(day_str, args, status_data)
                    success, timing = res.ok, res.timing
                    if success:
                        break

            if success:
                days_completed += 1
                cont["days_completed"] = days_completed
                cont["day_times"].append(timing)
                cont["day_times"] = cont["day_times"][-100:]
                current_day += timedelta(days=1)
                if res.partial:
                    state.add_partial(day_str)
                    cont["days_partial"] = list(state.days_partial)
                    save_state(OUTPUT_DIR, state)
                    # No email: the day is retried automatically; only a station
                    # dark for STATION_DARK_DAYS days is worth an alert.
                    print(f"  {day_str} ingested with gaps — will retry for up to "
                          f"{PARTIAL_MAX_AGE_DAYS} days")

                # Refresh the public site (best-effort — a failed export or
                # push must never affect the pipeline itself).
                try:
                    subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / "export_public_site.py"),
                         "--push"],
                        timeout=900, check=False,
                    )
                except Exception as exc:
                    print(f"  WARNING: public site export failed: {exc}")

                _check_station_coverage(state, date.fromisoformat(day_str))

                # Run monitoring checks every 10 days
                if days_completed % 10 == 0:
                    try:
                        from lib.monitoring import run_monitoring_checks

                        run_monitoring_checks(send_alert_fn=_send_alert)
                    except Exception as exc:
                        print(f"  WARNING: monitoring checks failed: {exc}")

                print()
            else:
                # ── persistent failure — skip the day and continue ───
                print(f"\n  SKIPPING {day_str} — failed after {MAX_RETRIES} retries")

                days_skipped.append(day_str)
                cont["days_skipped"] = days_skipped
                state.add_skipped(day_str)
                save_state(OUTPUT_DIR, state)
                current_day += timedelta(days=1)

                alert_subject = f"[El Paso Pipeline] SKIPPED — {day_str} failed"
                alert_body = (
                    f"Day {day_str} failed after {MAX_RETRIES} retries "
                    f"and was skipped.\n\n"
                    f"Days completed so far: {days_completed}\n"
                    f"Days skipped so far: {len(days_skipped)} "
                    f"({', '.join(days_skipped)})\n"
                    f"Pipeline continues with the next day.\n"
                )
                _notify(alert_subject, alert_body)

            _write_status(status_data)

    except KeyboardInterrupt:
        print("\nStopping continuous mode.")
        print(f"  Days completed: {days_completed}")
        if days_skipped:
            print(f"  Days skipped:   {', '.join(days_skipped)}")

        status_data["pipeline"]["status"] = "stopped"
        status_data["pipeline"]["finished_at"] = _now()
        cont["waiting"] = False
        cont["next_run_at"] = None
        _write_status(status_data)


# ── gap-fill mode ────────────────────────────────────────────────────────────


def gap_fill_run(args: argparse.Namespace) -> None:
    """Scan for per-station gaps in raw data and re-run the pipeline to fill them."""
    scan_start = date.fromisoformat(args.start) if args.start else PIPELINE_START
    scan_end = date.fromisoformat(args.end) if args.end else _target_date()

    print(f"Gap-fill mode: scanning {scan_start} to {scan_end}")
    stations = _load_station_expectations()
    print(f"  {len(stations)} stations loaded from stations.json")

    gaps = _scan_gaps(scan_start, scan_end, stations)

    if not gaps:
        print("\nNo gaps found — all stations have data for every expected day.")
        return

    # ── print summary ────────────────────────────────────────────────────
    total_missing = sum(len(missing) for _, missing in gaps)
    print(f"\nFound {len(gaps)} day(s) with gaps ({total_missing} total station-days missing):")
    for day, missing in gaps:
        print(f"  {day}  missing {len(missing)}: {', '.join(missing)}")
    print()

    if getattr(args, "dry_run", False):
        print("[dry-run] Would process the above gaps. Exiting.")
        return

    # ── fill gaps ────────────────────────────────────────────────────────
    archive_only = bool(getattr(args, "archive_only", False))
    if archive_only:
        # New-station backfill (RUNBOOK): fetch and process the history, but
        # do not re-detect/re-associate days that already have events —
        # re-association renumbers event IDs (ep{date}-{NNNN}) and would
        # orphan reviews. Nothing needs --force: only missing files are made.
        args.force = False
        only_steps: set[str] | None = {"ingest", "process"}
        print("Archive-only: running ingest + processing for the gap days (no re-detection).")
    else:
        args.force = True  # reprocess downstream steps with new data
        only_steps = None

    pipeline_started = _now()
    days_filled = 0
    days_failed: list[str] = []

    status_data: dict = {
        "pipeline": {
            "status": "running",
            "mode": "gap-fill",
            "started_at": pipeline_started,
            "finished_at": None,
            "args": {
                "start": scan_start.isoformat(),
                "end": scan_end.isoformat(),
                "force": True,
                "debug": args.debug,
                "start_step": 1,
                "end_step": len(STEPS),
            },
            "gap_fill": {
                "total_gap_days": len(gaps),
                "days_filled": 0,
                "days_failed": [],
            },
        },
        "steps": _make_step_statuses(),
    }
    _write_status(status_data)

    gf = status_data["pipeline"]["gap_fill"]

    for i, (day, missing) in enumerate(gaps, 1):
        day_str = day.isoformat()
        print(f"[{i}/{len(gaps)}] Filling {day_str}  (missing: {', '.join(missing)})")

        status_data["steps"] = _make_step_statuses()
        status_data["pipeline"]["status"] = "running"
        _write_status(status_data)

        res = _run_day(day_str, args, status_data, only_steps=only_steps)
        success, timing = res.ok, res.timing

        if not success:
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"  Retry {attempt}/{MAX_RETRIES} for {day_str} (waiting {RETRY_WAIT}s) ...")
                _time.sleep(RETRY_WAIT)
                status_data["steps"] = _make_step_statuses()
                status_data["pipeline"]["status"] = "running"
                _write_status(status_data)
                res = _run_day(day_str, args, status_data, only_steps=only_steps)
                success, timing = res.ok, res.timing
                if success:
                    break

        if success:
            days_filled += 1
            gf["days_filled"] = days_filled
            print(f"  Filled {day_str} ({_format_elapsed(timing['total'])})")
        else:
            days_failed.append(day_str)
            gf["days_failed"] = days_failed
            print(f"  FAILED {day_str} — skipping")

        _write_status(status_data)

    # ── final summary ────────────────────────────────────────────────────
    status_data["pipeline"]["status"] = "completed" if not days_failed else "completed_with_errors"
    status_data["pipeline"]["finished_at"] = _now()
    _write_status(status_data)

    print(
        f"\nGap-fill complete: {days_filled} filled, {len(days_failed)} failed "
        f"out of {len(gaps)} gap days."
    )
    if days_failed:
        print(f"  Failed days: {', '.join(days_failed)}")
        _send_alert(
            subject=f"[El Paso Pipeline] Gap-fill: {len(days_failed)} day(s) unfilled",
            body=(
                f"Gap-fill finished with {len(days_failed)} unfilled day(s):\n"
                f"{', '.join(days_failed)}\n\n"
                f"These days may need manual investigation.\n"
            ),
        )


# ── entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    args = parse_args()
    if args.validate:
        from lib.monitoring import validate_environment

        ok = validate_environment()
        sys.exit(0 if ok else 1)

    # Fail fast on environment drift (e.g. numpy upgraded past numba's ceiling).
    # Catches import-level breakage before any expensive work begins.
    from lib.monitoring import check_critical_imports

    imports_ok, import_errors = check_critical_imports()
    if not imports_ok:
        print("ERROR: critical imports failed -- the conda env is broken.", file=sys.stderr)
        for err in import_errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nLikely cause: an upstream package upgraded numpy past numba's ceiling.\n"
            "Try: conda install -n elpaso-quake -y -c conda-forge 'numpy<2.4'",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.gap_fill:
        gap_fill_run(args)
    elif args.continuous:
        continuous_run(args)
    else:
        single_run(args)


if __name__ == "__main__":
    main()
