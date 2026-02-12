"""
Monitoring and alerting module for the El Paso seismic pipeline.

Provides:
  - Disk space warnings (>80% full)
  - No new events in 48 hours alert
  - M > 4.0 immediate notification
  - Optional Slack/webhook integration via ALERT_WEBHOOK_URL env var
  - Environment validation (FDSNWS reachability, disk space, GPU, config)
"""

import csv
import json
import os
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
CATALOG_FILE = OUTPUT_DIR / "5-catalog" / "catalog.csv"
EVENTS_DIR = OUTPUT_DIR / "4-events"


# ── Webhook / Slack integration ─────────────────────────────────────────────


def send_webhook(subject: str, body: str) -> None:
    """Send an alert via webhook (Slack-compatible JSON payload).

    Reads ALERT_WEBHOOK_URL from environment.  Silently skips if not set.
    Compatible with Slack incoming webhooks, Discord webhooks, and generic
    HTTP endpoints that accept JSON POST.
    """
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return

    payload = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        print(f"  Webhook alert sent")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  WARNING: webhook alert failed: {exc}")


# ── Health checks ───────────────────────────────────────────────────────────


def check_disk_space(threshold_percent: float = 80.0) -> tuple[bool, str]:
    """Check if disk usage exceeds threshold.

    Returns (is_ok, message).
    """
    usage = shutil.disk_usage(ROOT)
    percent = (usage.used / usage.total) * 100 if usage.total else 0
    free_gb = usage.free / (1024**3)

    if percent >= threshold_percent:
        msg = (
            f"Disk usage at {percent:.1f}% ({free_gb:.1f} GB free). "
            f"Threshold: {threshold_percent}%"
        )
        return False, msg

    return True, f"Disk OK: {percent:.1f}% used, {free_gb:.1f} GB free"


def check_recent_events(hours: int = 48) -> tuple[bool, str]:
    """Check if any events have been detected in the last N hours.

    Returns (is_ok, message).
    """
    if not CATALOG_FILE.exists():
        return True, "No catalog file yet (pipeline may not have run)"

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    latest_event_time = None

    try:
        with open(CATALOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                t = row.get("time", "")
                if t:
                    try:
                        event_time = datetime.fromisoformat(t.replace("Z", "+00:00"))
                        if latest_event_time is None or event_time > latest_event_time:
                            latest_event_time = event_time
                    except ValueError:
                        continue
    except OSError:
        return True, "Could not read catalog file"

    if latest_event_time is None:
        return True, "No events in catalog yet"

    if latest_event_time < cutoff:
        hours_ago = (datetime.now(timezone.utc) - latest_event_time).total_seconds() / 3600
        msg = (
            f"No new events in {hours_ago:.0f} hours "
            f"(last event: {latest_event_time.isoformat()})"
        )
        return False, msg

    return True, f"Latest event: {latest_event_time.isoformat()}"


def check_significant_events(magnitude_threshold: float = 4.0) -> list[dict]:
    """Return recent catalog events above the magnitude threshold.

    Checks the last 24 hours of catalog entries.  Returns a list of
    event dicts for events that exceed the threshold.
    """
    if not CATALOG_FILE.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    significant = []

    try:
        with open(CATALOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                t = row.get("time", "")
                mag_str = row.get("magnitude", "")
                if not t or not mag_str:
                    continue
                try:
                    event_time = datetime.fromisoformat(t.replace("Z", "+00:00"))
                    mag = float(mag_str)
                except (ValueError, TypeError):
                    continue

                if event_time >= cutoff and mag >= magnitude_threshold:
                    significant.append({
                        "event_id": row.get("event_id", ""),
                        "time": t,
                        "magnitude": mag,
                        "latitude": row.get("latitude", ""),
                        "longitude": row.get("longitude", ""),
                        "depth_km": row.get("depth_km", ""),
                    })
    except OSError:
        pass

    return significant


# ── Validation (--validate flag) ────────────────────────────────────────────


def validate_environment() -> bool:
    """Run comprehensive environment validation.

    Checks:
      1. FDSNWS endpoint reachability
      2. Disk space
      3. GPU availability (optional)
      4. Config file validity
      5. Station file validity
      6. Output directory writability

    Returns True if all critical checks pass.
    """
    all_ok = True
    print("=" * 60)
    print("El Paso Seismic Pipeline -- Environment Validation")
    print("=" * 60)

    # 1. FDSNWS reachability
    print("\n[1/6] FDSNWS endpoint...")
    fdsnws_url = "https://data.raspberryshake.org/fdsnws/dataselect/1/version"
    try:
        with urllib.request.urlopen(fdsnws_url, timeout=15) as resp:
            version = resp.read().decode().strip()
            print(f"  OK: Raspberry Shake FDSNWS reachable (version: {version})")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  FAIL: Cannot reach FDSNWS: {exc}")
        all_ok = False

    # Also check IRIS if EP.KIDD station is configured
    iris_url = "https://service.iris.edu/fdsnws/dataselect/1/version"
    try:
        with urllib.request.urlopen(iris_url, timeout=15) as resp:
            version = resp.read().decode().strip()
            print(f"  OK: IRIS FDSNWS reachable (version: {version})")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  WARN: Cannot reach IRIS FDSNWS: {exc}")

    # 2. Disk space
    print("\n[2/6] Disk space...")
    ok, msg = check_disk_space()
    print(f"  {'OK' if ok else 'WARN'}: {msg}")
    if not ok:
        all_ok = False

    # 3. GPU availability
    print("\n[3/6] GPU availability...")
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            print(f"  OK: CUDA GPU available ({gpu_name})")
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            print(f"  OK: Apple MPS GPU available")
        else:
            print(f"  INFO: No GPU detected (CPU-only mode -- detection will be slower)")
    except ImportError:
        print(f"  WARN: PyTorch not installed -- cannot check GPU")

    # 4. Config files
    print("\n[4/6] Configuration files...")
    config_files = [
        "1-ingestion/config.yaml",
        "2-processing/config.yaml",
        "3-detection/config.yaml",
        "4-association/config.yaml",
        "5-catalog/config.yaml",
    ]
    for cf in config_files:
        path = ROOT / cf
        if path.exists():
            try:
                import yaml
                with open(path) as f:
                    yaml.safe_load(f)
                print(f"  OK: {cf}")
            except Exception as exc:
                print(f"  FAIL: {cf} -- {exc}")
                all_ok = False
        else:
            print(f"  FAIL: {cf} not found")
            all_ok = False

    # 5. Station file
    print("\n[5/6] Station file...")
    stations_path = ROOT / "stations.json"
    if stations_path.exists():
        try:
            stations = json.loads(stations_path.read_text())
            print(f"  OK: {len(stations)} stations configured")
        except Exception as exc:
            print(f"  FAIL: stations.json -- {exc}")
            all_ok = False
    else:
        print(f"  FAIL: stations.json not found")
        all_ok = False

    # 6. Output directory writability
    print("\n[6/6] Output directories...")
    for d in ["output", "logs"]:
        path = ROOT / d
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        try:
            test_file.write_text("test")
            test_file.unlink()
            print(f"  OK: {d}/ writable")
        except OSError as exc:
            print(f"  FAIL: {d}/ not writable -- {exc}")
            all_ok = False

    # Summary
    print("\n" + "=" * 60)
    if all_ok:
        print("VALIDATION PASSED -- pipeline is ready to run")
    else:
        print("VALIDATION FAILED -- fix the issues above before running")
    print("=" * 60)

    return all_ok


def run_monitoring_checks(send_alert_fn=None) -> dict:
    """Run all monitoring checks and send alerts as needed.

    Parameters
    ----------
    send_alert_fn : callable, optional
        Function with signature (subject, body) -> None for email alerts.
        If None, alerts are only printed to stdout.

    Returns a summary dict of check results.
    """
    results = {}

    # Disk space
    ok, msg = check_disk_space()
    results["disk_space"] = {"ok": ok, "message": msg}
    if not ok:
        subject = "[El Paso Pipeline] Disk space warning"
        body = f"Disk space check failed:\n{msg}\n\nFree up space or expand storage."
        print(f"  ALERT: {msg}")
        if send_alert_fn:
            send_alert_fn(subject, body)
        send_webhook(subject, body)

    # Recent events
    ok, msg = check_recent_events()
    results["recent_events"] = {"ok": ok, "message": msg}
    if not ok:
        subject = "[El Paso Pipeline] No new events in 48 hours"
        body = (
            f"Event detection gap:\n{msg}\n\n"
            f"This may indicate station outages, FDSNWS issues, or a quiet period."
        )
        print(f"  ALERT: {msg}")
        if send_alert_fn:
            send_alert_fn(subject, body)
        send_webhook(subject, body)

    # Significant events (M >= 4.0)
    significant = check_significant_events()
    results["significant_events"] = {"count": len(significant), "events": significant}
    for event in significant:
        subject = (
            f"[El Paso Pipeline] M{event['magnitude']:.1f} event detected"
        )
        body = (
            f"Significant earthquake detected:\n"
            f"  Event ID: {event['event_id']}\n"
            f"  Time: {event['time']}\n"
            f"  Magnitude: {event['magnitude']:.1f}\n"
            f"  Location: {event['latitude']}, {event['longitude']}\n"
            f"  Depth: {event['depth_km']} km\n"
        )
        print(f"  ALERT: M{event['magnitude']:.1f} event at {event['time']}")
        if send_alert_fn:
            send_alert_fn(subject, body)
        send_webhook(subject, body)

    return results
