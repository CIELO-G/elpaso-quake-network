"""Dashboard: old positional event ids still find their event."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import dashboard.routes.catalog as cr

ROWS = [
    {"event_id": "ep20251123T235949", "time": "2025-11-23T23:59:49.5", "review_status": ""},
    {"event_id": "ep20260608T100000", "time": "2026-06-08T10:00:00.4", "review_status": ""},
]
ALIASES = {"ep20251124-0000": "ep20251123T235949"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cr, "read_catalog", lambda: [dict(r) for r in ROWS])
    monkeypatch.setattr(cr, "read_assignments", lambda: [])
    monkeypatch.setattr(cr, "read_id_aliases", lambda: dict(ALIASES))
    monkeypatch.setattr(cr, "get_cached", lambda *a, **k: None)
    monkeypatch.setattr(cr, "set_cached", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(cr.router)
    return TestClient(app)


def test_search_matches_the_old_id(client):
    events = client.get("/api/catalog?search=20251124-0000").json()["events"]
    assert [e["event_id"] for e in events] == ["ep20251123T235949"]


def test_detail_by_old_id_returns_the_event_under_its_current_id(client):
    ev = client.get("/api/event/ep20251124-0000").json()["event"]
    assert ev["event_id"] == "ep20251123T235949" and ev["legacy_id"] == "ep20251124-0000"


def test_detail_of_an_event_without_an_old_id(client):
    assert client.get("/api/event/ep20260608T100000").json()["event"]["legacy_id"] is None
    assert client.get("/api/event/ep20990101-0000").status_code == 404
