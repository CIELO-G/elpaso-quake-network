#!/usr/bin/env python
"""
Pipeline orchestrator — runs all 5 seismic processing steps as subprocesses
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
import smtplib
import subprocess
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUS_FILE = ROOT / "output" / "pipeline_status.json"
EVENTS_DIR = ROOT / "output" / "4-events"
STATIONS_FILE = ROOT / "stations.json"
RAW_DIR = ROOT / "output" / "1-raw"

# ── continuous-mode defaults ──────────────────────────────────────────────────

LAG_HOURS = 6
MAX_RETRIES = 5
RETRY_WAIT = 120          # seconds between retries
PIPELINE_START = date(2025, 11, 1)
MAX_SKIP_RETRY_PASSES = 3   # retry all skipped days this many times after catching up
SKIP_RETRY_WAIT = 600       # 10-minute cooldown between retry passes

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


def _send_alert(subject: str, body: str) -> None:
    """Send an email alert.  Silently skips if env vars are not configured."""
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not to_addr:
        print("  (email alert skipped — ALERT_EMAIL_TO not set)")
        return

    from_addr = os.environ.get("ALERT_EMAIL_FROM", to_addr)
    host = os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("ALERT_SMTP_PORT", "465"))
    password = os.environ.get("ALERT_SMTP_PASSWORD", "")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as srv:
            if password:
                srv.login(from_addr, password)
            srv.sendmail(from_addr, [to_addr], msg.as_string())
        print(f"  Alert email sent to {to_addr}")
    except Exception as exc:
        print(f"  WARNING: failed to send alert email: {exc}")


# ── gap-fill helpers ─────────────────────────────────────────────────────────

def _load_station_expectations() -> list[tuple[str, str, date]]:
    """Read stations.json and return (network, station, start_date) tuples."""
    with open(STATIONS_FILE) as f:
        stations = json.load(f)
    return [
        (s["network"], s["station"], date.fromisoformat(s["start_date"]))
        for s in stations
    ]


def _scan_gaps(
    scan_start: date,
    scan_end: date,
    stations: list[tuple[str, str, date]],
) -> list[tuple[date, list[str]]]:
    """Find days with missing station data in 1-raw/.

    Returns list of (day, [missing_station_ids]) for days that have gaps.
    """
    gaps: list[tuple[date, list[str]]] = []
    day = scan_start
    while day <= scan_end:
        doy = day.timetuple().tm_yday
        day_dir = RAW_DIR / str(day.year) / f"{doy:03d}"

        # stations expected to be online this day
        expected = {f"{net}.{sta}" for net, sta, start in stations if start <= day}
        if not expected:
            day += timedelta(days=1)
            continue

        if not day_dir.exists():
            # entire day missing
            gaps.append((day, sorted(expected)))
        else:
            # parse NET.STA from mseed filenames
            present = set()
            for f in day_dir.iterdir():
                parts = f.name.split(".")
                if len(parts) >= 2:
                    present.add(f"{parts[0]}.{parts[1]}")
            missing = expected - present
            if missing:
                gaps.append((day, sorted(missing)))

        day += timedelta(days=1)
    return gaps


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
    p.add_argument("--validate", action="store_true",
                   help="Validate environment (FDSNWS, disk, GPU, config) and exit")
    p.add_argument("--gap-fill", action="store_true",
                   help="Scan for days with missing station data and re-run "
                        "the pipeline to fill gaps")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands that would be run, without executing them")
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
        if start and end and start == end:
            # Same date means "process this whole day", so end = next day
            end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
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
    """Auto-detect where to resume by checking the last day in 2-processed/.

    Uses processed output rather than events, since spillover directories
    (from traces crossing midnight) only appear in raw/picks/events but
    never in processed (the 1-sample files fail processing).
    """
    processed_dir = ROOT / "output" / "2-processed"
    if not processed_dir.exists():
        return PIPELINE_START
    latest = None
    for year_dir in processed_dir.iterdir():
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

        end_str = (date.fromisoformat(day_str) + timedelta(days=1)).isoformat()
        cmd = build_command(step, args,
                            start_override=day_str, end_override=end_str)
        t0 = datetime.now(timezone.utc)
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
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

            # ── retry skipped days before sleeping ─────────────────────
            if current_day > target and days_skipped:
                print(f"\nCaught up — retrying {len(days_skipped)} skipped day(s)...")
                still_skipped: list[str] = []

                for pass_num in range(1, MAX_SKIP_RETRY_PASSES + 1):
                    remaining = days_skipped if pass_num == 1 else still_skipped
                    if not remaining:
                        break
                    still_skipped = []
                    print(f"  Retry pass {pass_num}/{MAX_SKIP_RETRY_PASSES} "
                          f"({len(remaining)} day(s))")

                    for skip_day in remaining:
                        status_data["steps"] = _make_step_statuses()
                        status_data["pipeline"]["status"] = "running"
                        status_data["pipeline"]["args"]["start"] = skip_day
                        status_data["pipeline"]["args"]["end"] = skip_day
                        _write_status(status_data)

                        print(f"    Retrying {skip_day} ...")
                        ok, timing = _run_day(skip_day, args, status_data)

                        if not ok:
                            # one more attempt after a short wait
                            _time.sleep(RETRY_WAIT)
                            status_data["steps"] = _make_step_statuses()
                            _write_status(status_data)
                            ok, timing = _run_day(skip_day, args, status_data)

                        if ok:
                            print(f"    {skip_day} recovered!")
                            days_completed += 1
                            cont["days_completed"] = days_completed
                            cont["day_times"].append(timing)
                            cont["day_times"] = cont["day_times"][-100:]
                        else:
                            print(f"    {skip_day} still failing")
                            still_skipped.append(skip_day)

                    if still_skipped and pass_num < MAX_SKIP_RETRY_PASSES:
                        print(f"  Waiting {SKIP_RETRY_WAIT}s before next pass...")
                        _time.sleep(SKIP_RETRY_WAIT)

                days_skipped = still_skipped
                cont["days_skipped"] = days_skipped
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
                print(f"\n  SKIPPING {day_str} — failed after "
                      f"{MAX_RETRIES} retries")

                days_skipped.append(day_str)
                cont["days_skipped"] = days_skipped
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
                _send_alert(subject=alert_subject, body=alert_body)
                try:
                    from lib.monitoring import send_webhook
                    send_webhook(alert_subject, alert_body)
                except Exception:
                    pass

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
    args.force = True  # reprocess downstream steps with new data

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
                "end_step": 5,
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
        print(f"[{i}/{len(gaps)}] Filling {day_str}  "
              f"(missing: {', '.join(missing)})")

        status_data["steps"] = _make_step_statuses()
        status_data["pipeline"]["status"] = "running"
        _write_status(status_data)

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

    print(f"\nGap-fill complete: {days_filled} filled, {len(days_failed)} failed "
          f"out of {len(gaps)} gap days.")
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
    if args.gap_fill:
        gap_fill_run(args)
    elif args.continuous:
        continuous_run(args)
    else:
        single_run(args)


if __name__ == "__main__":
    main()
