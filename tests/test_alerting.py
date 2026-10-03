"""A7: alert channels, station-dark detection, watchdog exhaustion alert."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import run_pipeline as rp
from dashboard import appmode
from lib import monitoring
from lib.pipeline_state import PipelineState, load_state


def test_alerting_status_reads_env(monkeypatch):
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert monitoring.alerting_status() == {"email": False, "webhook": False}
    monkeypatch.setenv("ALERT_EMAIL_TO", "a@b.c")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")
    assert monitoring.alerting_status() == {"email": True, "webhook": False}


def test_notify_uses_configured_channels(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(monitoring, "send_email", lambda s, b: sent.append(("email", s)))
    monkeypatch.setattr(monitoring, "send_webhook", lambda s, b: sent.append(("hook", s)))
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monitoring.notify("subj", "body")
    assert sent == [] and "no ALERT_* channel" in capsys.readouterr().out
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://x")
    monitoring.notify("subj", "body")
    assert sent == [("hook", "subj")]


def _raw_day(raw, d: date, *stations: str):
    p = raw / f"{d.year}" / f"{d.timetuple().tm_yday:03d}"
    p.mkdir(parents=True, exist_ok=True)
    for s in stations:
        (p / f"{s}.00.EHZ.x.mseed").write_text("x")


def test_stations_without_data(tmp_path):
    raw = tmp_path / "1-raw"
    last = date(2026, 10, 1)
    stations = [("AM", "R0F2D", date(2025, 1, 1)), ("AM", "DEAD1", date(2025, 1, 1)),
                ("AM", "NEW01", date(2026, 9, 30))]
    for i in range(3):
        _raw_day(raw, date(2026, 9, 29) + timedelta(days=i), "AM.R0F2D")
    dark = monitoring.stations_without_data(raw, stations, last, days=3)
    assert dark == ["AM.DEAD1"]  # NEW01 is too new to judge
    _raw_day(raw, date(2026, 9, 29), "AM.DEAD1")  # one file in the window → not dark
    assert monitoring.stations_without_data(raw, stations, last, days=3) == []


def test_check_station_coverage_alerts_on_transitions_only(tmp_path, monkeypatch):
    out = tmp_path / "output"
    raw = out / "1-raw"
    monkeypatch.setattr(rp, "OUTPUT_DIR", out)
    monkeypatch.setattr(rp, "RAW_DIR", raw)
    monkeypatch.setattr(rp, "_load_station_expectations",
                        lambda: [("AM", "R0F2D", date(2025, 1, 1)), ("AM", "DEAD1", date(2025, 1, 1))])
    notes = []
    monkeypatch.setattr(rp, "_notify", lambda s, b: notes.append(s))
    for i in range(3):
        _raw_day(raw, date(2026, 9, 29) + timedelta(days=i), "AM.R0F2D")
    st = PipelineState()
    rp._check_station_coverage(st, date(2026, 10, 1))
    assert len(notes) == 1 and "dark" in notes[0] and st.dark_stations == ["AM.DEAD1"]
    assert load_state(out).dark_stations == ["AM.DEAD1"]
    rp._check_station_coverage(st, date(2026, 10, 1))  # same state: no repeat
    assert len(notes) == 1
    _raw_day(raw, date(2026, 10, 1), "AM.DEAD1")
    rp._check_station_coverage(st, date(2026, 10, 1))
    assert len(notes) == 2 and "back" in notes[1] and st.dark_stations == []


def test_watchdog_alerts_once_when_budget_exhausted(monkeypatch):
    wd = appmode.PipelineWatchdog(port=1)
    wd.consecutive_restarts = appmode.MAX_RESTARTS
    states = iter([{"running": False, "user_stopped": False}] * 3 + [{"running": True}]
                  + [{"running": False, "user_stopped": False}] * 2)
    monkeypatch.setattr(appmode, "pipeline_state", lambda port: next(states))
    sent = []
    monkeypatch.setattr(monitoring, "notify", lambda s, b: sent.append(s))
    # poll without sleeping: stop after 6 iterations
    n = {"i": 0}

    def fake_wait(timeout=None):
        n["i"] += 1
        return n["i"] > 6
    monkeypatch.setattr(wd._stop_event, "wait", fake_wait)
    wd.run()
    assert len(sent) == 2  # once for the first outage, once after recovery+second outage
    assert all("watchdog gave up" in s for s in sent)
