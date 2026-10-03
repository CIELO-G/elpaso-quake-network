"""/api/catalog review_status=queue|auto_rejected filters + row parsing."""
from fastapi import FastAPI
from starlette.testclient import TestClient

import dashboard.routes.catalog as cr
from dashboard.deps import parse_catalog_row

ROWS = [
    dict(event_id="a", time="2026-01-01T00:00:00Z", review_status="", triage="ok", triage_reason=""),
    dict(event_id="b", time="2026-01-02T00:00:00Z", review_status="", triage="flag", triage_reason="4 picks (unconstrained)"),
    dict(event_id="c", time="2026-01-03T00:00:00Z", review_status="", triage="auto_reject", triage_reason="0 core station(s) < 2"),
    dict(event_id="d", time="2026-01-04T00:00:00Z", review_status="confirmed", triage="auto_reject", triage_reason="M2.5 from 4 picks"),
    dict(event_id="e", time="2026-01-05T00:00:00Z", review_status="", triage="", triage_reason=""),   # pre-triage row
]


def _client(monkeypatch):
    monkeypatch.setattr(cr, "read_catalog", lambda: [dict(r) for r in ROWS])
    monkeypatch.setattr(cr, "get_cached", lambda *a, **k: None)
    monkeypatch.setattr(cr, "set_cached", lambda *a, **k: None)
    app = FastAPI(); app.include_router(cr.router)
    return TestClient(app)


def _ids(resp):
    return sorted(e["event_id"] for e in resp.json()["events"])


def test_queue_excludes_auto_rejected_but_keeps_flag_and_unlabelled(monkeypatch):
    c = _client(monkeypatch)
    assert _ids(c.get("/api/catalog?review_status=queue")) == ["a", "b", "e"]


def test_auto_rejected_filter_is_unreviewed_only(monkeypatch):
    c = _client(monkeypatch)
    assert _ids(c.get("/api/catalog?review_status=auto_rejected")) == ["c"]   # d is confirmed


def test_unreviewed_filter_unchanged(monkeypatch):
    c = _client(monkeypatch)
    assert _ids(c.get("/api/catalog?review_status=unreviewed")) == ["a", "b", "c", "e"]


def test_parse_row_carries_triage_fields():
    p = parse_catalog_row(ROWS[2])
    assert p["triage"] == "auto_reject" and "core" in p["triage_reason"]
    assert parse_catalog_row({"event_id": "x"})["triage"] == ""
