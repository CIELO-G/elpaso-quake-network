#!/usr/bin/env python
"""
Pipeline orchestrator — runs all 5 seismic processing steps as subprocesses
and writes status to output/pipeline_status.json for the dashboard to read.

Supports two modes:
  Single-run:  python run_pipeline.py --start 2026-01-01 --end 2026-01-01
  Continuous:  python run_pipeline.py --continuous --start 2025-11-01
"""

import argparse
import json
import os
import subprocess
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUS_FILE = ROOT / "output" / "pipeline_status.json"
EVENTS_DIR = ROOT / "output" / "4-events"

# ── continuous-mode defaults ──────────────────────────────────────────────────

LAG_HOURS = 6
MAX_RETRIES = 2
RETRY_WAIT = 120          # seconds between retries
PIPELINE_START = date(2025, 11, 1)

# ── step registry ────────────────────────────────────────────────────────────

STEPS = [
    {
        "number": 1,
        "name": "ingest",
        "script": "1-ingestion/ingest.py",
        "config": "1-ingestion/config.yaml",
        "log_file": "logs/ingest.log",
        "accepts_dates": True,
        "accepts_force": False,
        "accepts_rebuild": False,
    },
    {
        "number": 2,
        "name": "process",
        "script": "2-processing/process.py",
        "config": "2-processing/config.yaml",
        "log_file": "logs/process.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        "number": 3,
        "name": "detect",
        "script": "3-detection/detect.py",
        "config": "3-detection/config.yaml",
        "log_file": "logs/detect.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        "number": 4,
        "name": "associate",
        "script": "4-association/associate.py",
        "config": "4-association/config.yaml",
        "log_file": "logs/associate.log",
        "accepts_dates": True,
        "accepts_force": True,
        "accepts_rebuild": False,
    },
    {
        "number": 5,
        "name": "catalog",
        "script": "5-catalog/catalog.py",
        "config": "5-catalog/config.yaml",
        "log_file": "logs/catalog.log",
        "accepts_dates": False,
        "accepts_force": False,
        "accepts_rebuild": True,
    },
]

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
            "number": s["number"],
            "name": s["name"],
            "status": initial,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "log_file": s["log_file"],
        }
        for s in STEPS
    ]

# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the seismic processing pipeline.")
    p.add_argument("--start", help="Start date (forwarded to steps 1-4)")
    p.add_argument("--end", help="End date (forwarded to steps 1-4)")
    p.add_argument("--force", action="store_true",
                   help="Force reprocessing (--force for steps 2-4, --rebuild for step 5)")
    p.add_argument("--debug", action="store_true", help="Enable debug logging in all steps")
    p.add_argument("--start-step", type=int, default=1, metavar="N",
                   help="First step to run (default: 1)")
    p.add_argument("--end-step", type=int, default=5, metavar="N",
                   help="Last step to run (default: 5)")
    p.add_argument("--continuous", action="store_true",
                   help="Run continuously — process one day at a time, "
                        "then wait for new data (Ctrl+C to stop)")
    return p.parse_args()


def build_command(step: dict, args: argparse.Namespace, *,
                  start_override: str | None = None,
                  end_override: str | None = None) -> list[str]:
    """Build the subprocess command list for a step."""
    cmd = [sys.executable, str(ROOT / step["script"]),
           "--config", str(ROOT / step["config"])]
    if step["accepts_dates"]:
        start = start_override or args.start
        end = end_override or args.end
        if start:
            cmd += ["--start", start]
        if end:
            cmd += ["--end", end]
    if args.force:
        if step["accepts_force"]:
            cmd.append("--force")
        elif step["accepts_rebuild"]:
            cmd.append("--rebuild")
    if args.debug:
        cmd.append("--debug")
    return cmd


# ── single-run mode (original behaviour) ─────────────────────────────────────

def single_run(args: argparse.Namespace) -> None:
    if args.start_step < 1 or args.end_step > 5 or args.start_step > args.end_step:
        print(f"Error: invalid step range {args.start_step}-{args.end_step}", file=sys.stderr)
        sys.exit(1)

    # ── build initial status ─────────────────────────────────────────────
    pipeline_started = _now()
    step_statuses = []
    for step in STEPS:
        n = step["number"]
        if n < args.start_step or n > args.end_step:
            status = "skipped"
        else:
            status = "pending"
        step_statuses.append({
            "number": n,
            "name": step["name"],
            "status": status,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "log_file": step["log_file"],
        })

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

    for step in STEPS:
        n = step["number"]
        if n < args.start_step or n > args.end_step:
            continue

        idx = n - args.start_step + 1
        print(f"[{idx}/{total}] Running {step['name']} ...")

        # mark running
        step_statuses[n - 1]["status"] = "running"
        step_statuses[n - 1]["started_at"] = _now()
        _write_status(status_data)

        # execute
        cmd = build_command(step, args)
        t0 = datetime.now(timezone.utc)
        result = subprocess.run(cmd)
        t1 = datetime.now(timezone.utc)
        elapsed = (t1 - t0).total_seconds()

        # record result
        step_statuses[n - 1]["finished_at"] = _now()
        step_statuses[n - 1]["return_code"] = result.returncode

        if result.returncode == 0:
            step_statuses[n - 1]["status"] = "completed"
            print(f"[{idx}/{total}] Completed in {_format_elapsed(elapsed)}")
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

def _find_next_day() -> date:
    """Auto-detect where to resume by checking the last day in 4-events/."""
    if not EVENTS_DIR.exists():
        return PIPELINE_START
    latest = None
    for year_dir in EVENTS_DIR.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        for doy_dir in year_dir.iterdir():
            if not doy_dir.is_dir() or not doy_dir.name.isdigit():
                continue
            doy = int(doy_dir.name)
            d = date(year, 1, 1) + timedelta(days=doy - 1)
            if latest is None or d > latest:
                latest = d
    if latest is None:
        return PIPELINE_START
    return latest + timedelta(days=1)


def _run_day(day_str: str, args: argparse.Namespace,
             status_data: dict) -> tuple[bool, dict]:
    """Run all 5 pipeline steps for a single day.

    Updates ``status_data["steps"]`` in-place so the dashboard sees
    real-time step progress.  Returns ``(success, timing_dict)``.
    """
    steps = status_data["steps"]
    timing: dict = {"day": day_str, "total": 0.0, "steps": {}}

    for i, step in enumerate(STEPS):
        steps[i]["status"] = "running"
        steps[i]["started_at"] = _now()
        steps[i]["finished_at"] = None
        steps[i]["return_code"] = None
        _write_status(status_data)

        cmd = build_command(step, args,
                            start_override=day_str, end_override=day_str)
        t0 = datetime.now(timezone.utc)
        result = subprocess.run(cmd)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

        timing["steps"][step["name"]] = round(elapsed, 1)

        steps[i]["finished_at"] = _now()
        steps[i]["return_code"] = result.returncode

        if result.returncode == 0:
            steps[i]["status"] = "completed"
            print(f"  {step['name']}: completed ({_format_elapsed(elapsed)})")
        else:
            steps[i]["status"] = "failed"
            print(f"  {step['name']}: FAILED (exit {result.returncode})")
            timing["total"] = round(sum(timing["steps"].values()), 1)
            _write_status(status_data)
            return False, timing

        _write_status(status_data)

    timing["total"] = round(sum(timing["steps"].values()), 1)
    return True, timing


def continuous_run(args: argparse.Namespace) -> None:
    """Process days one at a time, sleeping when caught up."""
    if args.start:
        current_day = date.fromisoformat(args.start)
    else:
        current_day = _find_next_day()

    days_completed = 0
    days_skipped: list[str] = []
    pipeline_started = _now()

    print(f"Continuous mode: starting from {current_day}")
    print(f"Lag: {LAG_HOURS}h | Retries: {MAX_RETRIES} | Ctrl+C to stop")
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
                "end_step": 5,
            },
            "continuous": {
                "current_day": current_day.isoformat(),
                "days_completed": 0,
                "days_skipped": [],
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

            # ── caught up — sleep until next day is eligible ──────────
            if current_day > target:
                next_run = datetime(
                    current_day.year, current_day.month, current_day.day,
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

                print(f"Caught up — waiting for {current_day} "
                      f"(~{_format_elapsed(wait_sec)})")

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
            success, timing = _run_day(day_str, args, status_data)

            if not success:
                for attempt in range(1, MAX_RETRIES + 1):
                    print(f"  Retry {attempt}/{MAX_RETRIES} for {day_str} "
                          f"(waiting {RETRY_WAIT}s) ...")
                    _time.sleep(RETRY_WAIT)
                    status_data["steps"] = _make_step_statuses()
                    status_data["pipeline"]["status"] = "running"
                    _write_status(status_data)
                    success, timing = _run_day(day_str, args, status_data)
                    if success:
                        break

            if success:
                days_completed += 1
                cont["days_completed"] = days_completed
                cont["day_times"].append(timing)
                cont["day_times"] = cont["day_times"][-100:]
                current_day += timedelta(days=1)
                print()
            else:
                print(f"  SKIPPING {day_str} after {MAX_RETRIES} retries\n")
                days_skipped.append(day_str)
                cont["days_skipped"] = list(days_skipped)
                current_day += timedelta(days=1)

            _write_status(status_data)

    except KeyboardInterrupt:
        print(f"\nStopping continuous mode.")
        print(f"  Days completed: {days_completed}")
        if days_skipped:
            print(f"  Days skipped:   {', '.join(days_skipped)}")

        status_data["pipeline"]["status"] = "stopped"
        status_data["pipeline"]["finished_at"] = _now()
        cont["waiting"] = False
        cont["next_run_at"] = None
        _write_status(status_data)


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    if args.continuous:
        continuous_run(args)
    else:
        single_run(args)


if __name__ == "__main__":
    main()
