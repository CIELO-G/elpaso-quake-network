"""Export endpoints: rejected events excluded, classification included."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import dashboard.routes.export as ex

ROWS = [
    dict(event_id="ep20260101-0000", time="2026-01-01T17:02:03.000Z",
         magnitude="1.2", magnitude_type="ML", latitude="31.9", longitude="-106.4",
         depth_km="0.1", num_picks="6", reviewed="True",
         review_status="confirmed", event_type="quarry_blast"),
    dict(event_id="ep20260102-0000", time="2026-01-02T09:00:00.000Z",
         magnitude="2.0", magnitude_type="ML", latitude="31.8", longitude="-106.5",
         depth_km="5.0", num_picks="8", reviewed="True",
         review_status="confirmed", event_type="earthquake"),
    dict(event_id="ep20260103-0000", time="2026-01-03T12:00:00.000Z",
         magnitude="0.9", magnitude_type="ML", latitude="31.7", longitude="-106.3",
         depth_km="0.0", num_picks="4", reviewed="True",
         review_status="rejected", event_type=""),
    dict(event_id="ep20260104-0000", time="2026-01-04T15:00:00.000Z",
         magnitude="1.1", magnitude_type="ML", latitude="31.6", longitude="-106.2",
         depth_km="0.2", num_picks="5", reviewed="False",
         review_status="unreviewed", event_type=""),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ex, "read_catalog", lambda: [dict(r) for r in ROWS])
    monkeypatch.setattr(ex, "read_assignments", lambda: [])
    app = FastAPI()
    app.include_router(ex.router)
    return TestClient(app)


def _csv_rows(resp) -> list[dict]:
    return list(csv.DictReader(io.StringIO(resp.text)))


def test_csv_excludes_rejected(client):
    rows = _csv_rows(client.get("/api/catalog/export"))
    ids = [r["event_id"] for r in rows]
    assert "ep20260103-0000" not in ids
    assert len(rows) == 3  # confirmed x2 + unreviewed survive


def test_csv_has_event_type_column(client):
    rows = _csv_rows(client.get("/api/catalog/export"))
    by_id = {r["event_id"]: r for r in rows}
    assert by_id["ep20260101-0000"]["event_type"] == "quarry_blast"
    assert by_id["ep20260102-0000"]["event_type"] == "earthquake"


def test_csv_404_when_all_rejected(client, monkeypatch):
    only_rejected = [r for r in ROWS if r["review_status"] == "rejected"]
    monkeypatch.setattr(ex, "read_catalog", lambda: [dict(r) for r in only_rejected])
    assert client.get("/api/catalog/export").status_code == 404


def test_quakeml_excludes_rejected_and_maps_type(client):
    resp = client.get("/api/catalog/export/quakeml")
    assert resp.status_code == 200
    xml = resp.text
    assert "ep20260103-0000" not in xml
    assert "quarry blast" in xml       # QuakeML enum value, with space
    assert "quarry_blast" not in xml   # raw catalog spelling must not leak
    assert xml.count("<event ") == 3


def test_date_filter_still_applies(client):
    rows = _csv_rows(client.get("/api/catalog/export?start_date=2026-01-02&end_date=2026-01-02"))
    assert [r["event_id"] for r in rows] == ["ep20260102-0000"]
