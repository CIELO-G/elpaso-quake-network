"""run_pipeline.py orchestrator: resume point, gap scan, partial days, persisted state."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import run_pipeline as rp
from lib.pipeline_state import PipelineState, load_state, save_state


def _mk_day(base, d: date, *files: str):
    doy = d.timetuple().tm_yday
    p = base / f"{d.year}" / f"{doy:03d}"
    p.mkdir(parents=True, exist_ok=True)
    for f in files:
        (p / f.format(y=d.year, j=f"{doy:03d}")).write_text("x")
    return p


@pytest.fixture
def out(tmp_path, monkeypatch):
    o = tmp_path / "output"
    monkeypatch.setattr(rp, "OUTPUT_DIR", o)
    monkeypatch.setattr(rp, "STATUS_FILE", o / "pipeline_status.json")
    monkeypatch.setattr(rp, "RAW_DIR", o / "1-raw")
    monkeypatch.setattr(rp, "PROCESSED_DIR", o / "2-processed")
    monkeypatch.setattr(rp, "EVENTS_DIR", o / "4-events")
    return o


# ── resume point ──────────────────────────────────────────────────────────

def test_resume_ignores_processed_day_without_events(out):
    d1, d2 = date(2026, 9, 26), date(2026, 9, 27)
    _mk_day(out / "2-processed", d1, "a.mseed")
    _mk_day(out / "4-events", d1, "{y}.{j}.events.csv")
    _mk_day(out / "2-processed", d2, "a.mseed")  # crashed before events
    assert rp._find_next_day() == d2  # re-run the unfinished day, not skip it


def test_resume_ignores_spillover_events_dir(out):
    d1, d2 = date(2026, 9, 26), date(2026, 9, 27)
    _mk_day(out / "2-processed", d1, "a.mseed")
    _mk_day(out / "4-events", d1, "{y}.{j}.events.csv")
    _mk_day(out / "4-events", d2, "{y}.{j}.events.csv")  # spill-over, never processed
    assert rp._find_next_day() == d2


def test_resume_from_scratch(out):
    assert rp._find_next_day() == rp.PIPELINE_START


# ── gap scan ──────────────────────────────────────────────────────────────

def test_scan_gaps_flags_missing_station_and_unfinished_day(out):
    d = date(2026, 9, 26)
    stations = [("AM", "R0F2D", date(2025, 1, 1)), ("AM", "R4B41", date(2025, 1, 1))]
    _mk_day(out / "2-processed", d, "AM.R0F2D.00.EHZ.2026.269.mseed")
    gaps = rp._scan_gaps(d, d, stations)
    assert gaps == [(d, ["AM.R4B41"])]
    # all stations present but no events file → pipeline incomplete
    _mk_day(out / "2-processed", d, "AM.R4B41.00.EHZ.2026.269.mseed")
    gaps = rp._scan_gaps(d, d, stations)
    assert gaps and "pipeline incomplete" in gaps[0][1][0]
    _mk_day(out / "4-events", d, "{y}.{j}.events.csv")
    assert rp._scan_gaps(d, d, stations) == []


# ── _run_day: partial ingest ──────────────────────────────────────────────

def _args(force=False):
    return argparse.Namespace(start=None, end=None, force=force, debug=False)


def _status():
    return {"pipeline": {"args": {}}, "steps": rp._make_step_statuses()}


def test_run_day_partial_ingest_continues_and_flags(out, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        rc = rp.INGEST_EXIT_PARTIAL if "ingest.py" in cmd[1] else 0
        return SimpleNamespace(returncode=rc)

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    res = rp._run_day("2026-09-27", _args(), _status())
    assert res.ok and res.partial
    assert len(calls) == 5  # all steps ran
    assert "--force" not in calls[1]


def test_run_day_force_override_adds_force_and_rebuild(out, monkeypatch):
    calls = []
    monkeypatch.setattr(rp.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), SimpleNamespace(returncode=0))[1])
    rp._run_day("2026-09-27", _args(force=False), _status(), force=True)
    assert "--force" in calls[1] and "--force" in calls[2] and "--force" in calls[3]
    assert "--rebuild" in calls[4]


def test_run_day_failure_stops_and_is_not_partial(out, monkeypatch):
    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=1 if "associate.py" in cmd[1] else 0)

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    st = _status()
    res = rp._run_day("2026-09-27", _args(), st)
    assert not res.ok and not res.partial
    assert st["steps"][3]["status"] == "failed" and st["steps"][4]["status"] == "pending"


# ── persisted state ───────────────────────────────────────────────────────

def test_state_roundtrip_and_dedup(tmp_path):
    st = PipelineState()
    st.add_skipped("2026-09-02"); st.add_skipped("2026-09-01"); st.add_skipped("2026-09-02")
    st.add_partial("2026-09-05")
    save_state(tmp_path, st)
    back = load_state(tmp_path)
    assert back.days_skipped == ["2026-09-01", "2026-09-02"]
    assert back.days_partial == ["2026-09-05"]
    assert back.updated_at
    back.remove_skipped("2026-09-01"); back.remove_partial("2026-09-05")
    assert back.days_skipped == ["2026-09-02"] and back.days_partial == []
    assert load_state(tmp_path / "nowhere").days_skipped == []
    (tmp_path / "pipeline_state.json").write_text("{not json")
    assert load_state(tmp_path).days_skipped == []


def test_retry_partial_days_clears_when_clean_and_ages_out(out, monkeypatch):
    today = date(2026, 10, 2)
    monkeypatch.setattr(rp, "_target_date", lambda: today)
    notes = []
    monkeypatch.setattr(rp, "_notify", lambda s, b: notes.append(s))
    rc_by_day = {"2026-10-01": 0, "2026-09-30": rp.INGEST_EXIT_PARTIAL}

    def fake_run(cmd, **kw):
        if "ingest.py" in cmd[1]:
            day = cmd[cmd.index("--start") + 1]
            return SimpleNamespace(returncode=rc_by_day[day])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    st = PipelineState(days_partial=["2026-09-01", "2026-09-30", "2026-10-01"])
    save_state(out, st)
    rp._retry_partial_days(_args(), _status(), st)
    assert st.days_partial == ["2026-09-30"]  # 10-01 clean, 09-01 aged out
    assert any("remains incomplete" in n for n in notes)
    assert json.loads((out / "pipeline_state.json").read_text())["days_partial"] == ["2026-09-30"]
