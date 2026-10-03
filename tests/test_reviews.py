"""lib.reviews — append-only review history."""

from __future__ import annotations

import json

import numpy as np

from lib.reviews import append_review_records, jsonable, latest_records


def test_append_then_latest(tmp_path):
    side = tmp_path / "reviews.jsonl"
    assert append_review_records(side, {}, {}, source="x") == 0
    assert not side.exists()
    n = append_review_records(
        side,
        {"e1": {"event_id": "e1", "magnitude": np.float64(1.5), "ml_err": float("nan")}},
        {"e1": [{"station": "R0F2D", "phase": "P"}]},
        source="dashboard_save",
    )
    assert n == 1
    append_review_records(side, {"e1": {"event_id": "e1", "magnitude": 2.0}}, {}, source="rebuild")
    lines = side.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["source"] == "dashboard_save"
    assert first["event"]["magnitude"] == 1.5 and first["event"]["ml_err"] is None
    assert first["picks"][0]["station"] == "R0F2D"
    latest = latest_records(side)
    assert latest["e1"]["source"] == "rebuild" and latest["e1"]["event"]["magnitude"] == 2.0


def test_latest_skips_torn_line(tmp_path):
    side = tmp_path / "reviews.jsonl"
    append_review_records(side, {"e1": {"event_id": "e1"}}, {}, source="x")
    with side.open("a") as f:
        f.write('{"event_id": "e2", "event": {')  # crash mid-write
    assert set(latest_records(side)) == {"e1"}
    assert latest_records(tmp_path / "missing.jsonl") == {}


def test_jsonable_numpy_and_nan():
    out = jsonable({"a": np.int64(3), "b": float("nan"), "c": "x", "d": None})
    assert out == {"a": 3, "b": None, "c": "x", "d": None}
