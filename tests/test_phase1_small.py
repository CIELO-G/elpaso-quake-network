"""A8 prune guard, A9 stop reaper, A12 NaN amplitude."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── A12 ───────────────────────────────────────────────────────────────────

def test_station_ml_rejects_nan_and_none():
    from lib.magnitude import MLConfig, compute_ml_station

    cfg = MLConfig()
    assert compute_ml_station(float("nan"), 20.0, cfg) is None
    assert compute_ml_station(None, 20.0, cfg) is None
    assert compute_ml_station(0.0, 20.0, cfg) is None
    assert compute_ml_station(1e-6, 20.0, cfg) is not None


def test_event_ml_skips_nan_amplitudes():
    import importlib.util

    import pandas as pd

    spec = importlib.util.spec_from_file_location("assoc_mod", ROOT / "4-association/associate.py")
    assoc = importlib.util.module_from_spec(spec)
    sys.modules["assoc_mod"] = assoc
    spec.loader.exec_module(assoc)

    picks = pd.DataFrame([
        {"network": "AM", "station": "A", "amplitude": float("nan")},
        {"network": "AM", "station": "B", "amplitude": 2e-6},
    ])
    stations = {"AM.A": {"latitude": 31.8, "longitude": -106.4},
                "AM.B": {"latitude": 31.9, "longitude": -106.5}}
    assignments = [(0, 7, 1.0), (1, 7, 1.0)]
    import logging
    import yaml
    config = yaml.safe_load((ROOT / "4-association" / "config.yaml").read_text())
    ml, err, n = assoc.compute_event_ml(31.7, -106.3, 5.0, 7, assignments, picks, stations,
                                        config, logging.getLogger("t"))
    assert n == 1 and ml is not None and not math.isnan(ml)


# ── A8 ────────────────────────────────────────────────────────────────────

def test_prune_raw_refuses_when_raw_is_a_symlink(tmp_path):
    """Run the real script against a fake repo whose 1-raw points elsewhere."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "output").mkdir()
    target = tmp_path / "raid-raw"
    target.mkdir()
    (repo / "output" / "1-raw").symlink_to(target)
    script = repo / "scripts" / "prune_raw.py"
    script.write_text((ROOT / "scripts" / "prune_raw.py").read_text())
    r = subprocess.run([sys.executable, str(script), "--apply"], capture_output=True, text=True)
    assert r.returncode == 2 and "REFUSING" in r.stdout


# ── A9 ────────────────────────────────────────────────────────────────────

def test_reaper_keeps_pid_file_until_exit_then_kills(tmp_path, monkeypatch):
    import dashboard.routes.pipeline as pl

    pid_file = tmp_path / "pipeline.pid"
    monkeypatch.setattr(pl, "PIPELINE_PID_FILE", pid_file)
    proc = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    pid_file.write_text(str(proc.pid))
    monkeypatch.setattr(pl, "_pipeline_proc", proc)
    time.sleep(0.5)
    os.kill(proc.pid, 15)  # ignored by the child, like a pipeline mid-step
    t0 = time.monotonic()
    pl._reap(proc.pid, proc, grace_s=1.0)
    assert proc.poll() is not None  # killed after the grace period
    assert 1.0 <= time.monotonic() - t0 < 15
    assert not pid_file.exists()
    assert pl._pipeline_proc is None


def test_reaper_clears_promptly_when_process_exits(tmp_path, monkeypatch):
    import dashboard.routes.pipeline as pl

    pid_file = tmp_path / "pipeline.pid"
    monkeypatch.setattr(pl, "PIPELINE_PID_FILE", pid_file)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    pid_file.write_text(str(proc.pid))
    monkeypatch.setattr(pl, "_pipeline_proc", proc)
    pl._reap(proc.pid, proc, grace_s=30.0)
    assert proc.returncode == 0 and not pid_file.exists()
