"""Stage exit-code policy: failures must not look like completed days."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ingest = _load("ingest_stage", "1-ingestion/ingest.py")


def _t(success=0, no_data=0, failed=0, skipped=0):
    return {"success": success, "no_data": no_data, "failed": failed, "skipped": skipped}


def test_clean_run_is_ok():
    assert ingest.exit_code_for(_t(success=300, no_data=36), 0, 14) == ingest.EXIT_OK
    assert ingest.exit_code_for(_t(skipped=336), 0, 14) == ingest.EXIT_OK  # all already in DB


def test_some_failures_with_data_is_partial():
    assert ingest.exit_code_for(_t(success=300, failed=12), 0, 14) == ingest.EXIT_PARTIAL
    assert ingest.exit_code_for(_t(success=300), 1, 14) == ingest.EXIT_PARTIAL  # one station down


def test_nothing_obtained_is_total_failure():
    assert ingest.exit_code_for(_t(failed=336), 0, 14) == ingest.EXIT_TOTAL_FAILURE
    assert ingest.exit_code_for(_t(), 14, 14) == ingest.EXIT_TOTAL_FAILURE  # every server unreachable


def test_associate_exits_nonzero_on_failed_day(tmp_path, monkeypatch):
    """GaMMA failure: no events file is written and main() exits 1."""
    import json
    import subprocess

    out = tmp_path / "output"
    picks_dir = out / "3-picks" / "2026" / "270"
    picks_dir.mkdir(parents=True)
    (picks_dir / "2026.270.picks.csv").write_text(
        "network,station,location,channel,phase,time,probability,model,amplitude,amplitude_channel\n"
        "AM,R0F2D,00,EHZ,P,2026-09-27T00:00:01.000,0.9,phasenet,1e-6,EHZ\n"
    )
    stations = json.load(open(ROOT / "stations.json"))
    (tmp_path / "stations.json").write_text(json.dumps(stations))
    cfg = (ROOT / "4-association" / "config.yaml").read_text()
    cfg = cfg.replace('stations_file: "stations.json"', f'stations_file: "{tmp_path}/stations.json"')
    cfg = cfg.replace('input_dir: "output"', f'input_dir: "{out}"').replace('output_dir: "output"', f'output_dir: "{out}"')
    cfg = cfg.replace('log_dir: "logs"', f'log_dir: "{tmp_path}/logs"')
    (tmp_path / "assoc.yaml").write_text(cfg)
    # Make GaMMA blow up: a sitecustomize that replaces gamma.utils.association.
    sc = tmp_path / "sc"
    sc.mkdir()
    (sc / "sitecustomize.py").write_text(
        "import gamma.utils\n"
        "def _boom(*a, **k): raise RuntimeError('simulated GaMMA failure')\n"
        "gamma.utils.association = _boom\n"
    )
    env = {"PYTHONPATH": str(sc), "PATH": "/usr/bin:/bin"}
    r = subprocess.run(
        [sys.executable, str(ROOT / "4-association" / "associate.py"), "--config", str(tmp_path / "assoc.yaml"),
         "--start", "2026-09-27", "--end", "2026-09-28"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert not (out / "4-events" / "2026" / "270" / "2026.270.events.csv").exists()


def test_associate_io_overrides_redirect_picks_events_and_log(tmp_path):
    """--input-dir/--output-dir/--log-name: how the PhaseNO step reuses association."""
    import json
    import subprocess

    alt = tmp_path / "phaseno"
    picks_dir = alt / "3-picks" / "2026" / "270"
    picks_dir.mkdir(parents=True)
    (picks_dir / "2026.270.picks.csv").write_text(
        "network,station,location,channel,phase,time,probability,model,amplitude,amplitude_channel\n"
    )
    (tmp_path / "stations.json").write_text(json.dumps(json.load(open(ROOT / "stations.json"))))
    cfg = (ROOT / "4-association" / "config.yaml").read_text()
    cfg = cfg.replace('stations_file: "stations.json"', f'stations_file: "{tmp_path}/stations.json"')
    cfg = cfg.replace('input_dir: "output"', f'input_dir: "{tmp_path}/unused"')
    cfg = cfg.replace('output_dir: "output"', f'output_dir: "{tmp_path}/unused"')
    cfg = cfg.replace('log_dir: "logs"', f'log_dir: "{tmp_path}/logs"')
    (tmp_path / "assoc.yaml").write_text(cfg)
    r = subprocess.run(
        [sys.executable, str(ROOT / "4-association" / "associate.py"), "--config", str(tmp_path / "assoc.yaml"),
         "--start", "2026-09-27", "--end", "2026-09-28", "--input-dir", str(alt),
         "--output-dir", str(alt), "--log-name", "associate_phaseno"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (alt / "4-events" / "2026" / "270" / "2026.270.events.csv").exists()
    assert not (tmp_path / "unused").exists()
    assert (tmp_path / "logs" / "associate_phaseno.log").exists()
    assert not (tmp_path / "logs" / "associate.log").exists()
