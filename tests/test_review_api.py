"""Dashboard save endpoints: catalog rewrite + reviews.jsonl append under the lock."""

from __future__ import annotations

import csv
import json

import pytest
from starlette.testclient import TestClient

import dashboard.deps as deps
import dashboard.routes.review as rv
from dashboard.app import app

CATALOG_COLS = [
    "event_id", "time", "magnitude", "magnitude_type", "ml_err", "latitude", "longitude",
    "depth_km", "sigma_time", "sigma_amp", "num_picks", "num_ml_sta", "reviewed",
    "review_status", "event_type",
]
ROW = {
    "event_id": "ep20260927-3", "time": "2026-09-27T23:46:12.788", "magnitude": "2.00",
    "magnitude_type": "ML", "ml_err": "0.86", "latitude": "31.674977", "longitude": "-105.799101",
    "depth_km": "30.00", "sigma_time": "0.3903", "sigma_amp": "0.7584", "num_picks": "4",
    "num_ml_sta": "4", "reviewed": "", "review_status": "", "event_type": "",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cat = tmp_path / "5-catalog" / "catalog.csv"
    cat.parent.mkdir()
    with cat.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CATALOG_COLS)
        w.writeheader()
        w.writerow(ROW)
    asg = tmp_path / "5-catalog" / "assignments.csv"
    with asg.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rv._ASSIGNMENT_COLUMNS)
        w.writeheader()
        w.writerow({"event_id": "ep20260927-3", "network": "AM", "station": "R0F2D",
                    "location": "00", "channel": "EHZ", "phase": "P",
                    "time": "2026-09-27T23:46:20.0", "probability": "0.9",
                    "amplitude": "1e-6", "amplitude_channel": "EHZ"})
    for mod in (deps, rv):
        monkeypatch.setattr(mod, "CATALOG_FILE", cat)
        monkeypatch.setattr(mod, "ASSIGNMENTS_FILE", asg)
    monkeypatch.setattr(rv, "_REVIEWS_SIDECAR", cat.parent / "reviews.jsonl")
    return TestClient(app), cat, asg


def _rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def test_status_update_writes_catalog_and_sidecar(client):
    c, cat, asg = client
    r = c.post("/api/event/ep20260927-3/review-status", json={"status": "rejected", "event_type": "noise"})
    assert r.status_code == 200, r.text
    row = _rows(cat)[0]
    assert row["review_status"] == "rejected" and row["event_type"] == "noise" and row["reviewed"]
    recs = [json.loads(l) for l in (cat.parent / "reviews.jsonl").read_text().splitlines()]
    assert len(recs) == 1
    assert recs[0]["source"] == "dashboard_status"
    assert recs[0]["event"]["review_status"] == "rejected"
    assert recs[0]["picks"][0]["station"] == "R0F2D"


def test_save_replaces_picks_and_appends_sidecar(client):
    c, cat, asg = client
    body = {
        "location": {"latitude": 31.7, "longitude": -106.4, "depth_km": 5.0,
                     "time": "2026-09-27T23:46:12.500", "num_picks": 2},
        "magnitude": {"ml": 1.8, "ml_err": 0.3, "num_ml_sta": 2},
        "review_status": "confirmed", "event_type": "earthquake",
        "picks": [
            {"network": "AM", "station": "R4B41", "channel": "EHZ", "phase": "P", "time": "t1"},
            {"network": "AM", "station": "R4B41", "channel": "EHN", "phase": "S", "time": "t2"},
        ],
    }
    r = c.post("/api/event/ep20260927-3/save", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["saved"] is True
    row = _rows(cat)[0]
    assert row["latitude"] == "31.7" and row["review_status"] == "confirmed" and row["magnitude"] == "1.8"
    picks = _rows(asg)
    assert [p["station"] for p in picks] == ["R4B41", "R4B41"]
    rec = json.loads((cat.parent / "reviews.jsonl").read_text().splitlines()[-1])
    assert rec["source"] == "dashboard_save"
    assert rec["event"]["latitude"] == "31.7"
    assert [p["phase"] for p in rec["picks"]] == ["P", "S"]


def test_unknown_event_is_404_and_writes_nothing(client):
    c, cat, asg = client
    r = c.post("/api/event/nope/review-status", json={"status": "confirmed"})
    assert r.status_code == 404
    assert not (cat.parent / "reviews.jsonl").exists()


def test_save_returns_503_when_pipeline_holds_lock(client):
    c, cat, asg = client
    from lib.catalog_lock import catalog_lock
    import threading

    held = threading.Event()
    release = threading.Event()

    def holder():
        with catalog_lock(cat.parent, timeout=5):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        rv._CATALOG_LOCK_TIMEOUT_S, saved = 0.2, rv._CATALOG_LOCK_TIMEOUT_S
        try:
            r = c.post("/api/event/ep20260927-3/review-status", json={"status": "confirmed"})
        finally:
            rv._CATALOG_LOCK_TIMEOUT_S = saved
        assert r.status_code == 503
        assert _rows(cat)[0]["review_status"] == ""
    finally:
        release.set()
        t.join(5)
