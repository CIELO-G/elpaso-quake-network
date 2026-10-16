"""Stable, time-based event ids (5-catalog/catalog.py).

An id is issued once, from the origin time of an event's first solution, and
a rebuild hands every re-found event its old id, reviewed or not.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "5-catalog"))
import catalog  # noqa: E402

LOG = logging.getLogger("test_event_ids")
EVENT_COLS = "event_index,time,magnitude,latitude,longitude,depth_km,num_picks"
ASSIGN_COLS = "event_index,network,station,location,channel,phase,time,probability,amplitude,amplitude_channel"


def frame(*rows: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame([{"event_id": i, "time": t} for i, t in rows])


# ── carry_over_ids ────────────────────────────────────────────────────


def test_refound_events_keep_previous_ids_and_their_picks_follow():
    fresh = frame(
        ("ep20260608T100001", "2026-06-08T10:00:01.0"),
        ("ep20260608T120000", "2026-06-08T12:00:00.0"),
    )
    picks = pd.DataFrame(
        {"event_id": ["ep20260608T100001", "ep20260608T120000"], "station": ["A", "B"]}
    )
    previous = frame(("ep20260608-0003", "2026-06-08T09:59:55.0Z"))
    cat, asg = catalog.carry_over_ids(fresh, picks, previous, LOG)
    assert cat["event_id"].tolist() == ["ep20260608-0003", "ep20260608T120000"]
    assert asg["event_id"].tolist() == ["ep20260608-0003", "ep20260608T120000"]


@pytest.mark.parametrize("dt_s, kept", [(10.0, True), (10.5, False)])
def test_same_event_window_is_reclaim_dt(dt_s, kept):
    t = pd.Timestamp("2026-06-08T10:00:00") + pd.Timedelta(seconds=dt_s)
    fresh = frame(("ep-fresh", t.isoformat()))
    cat, _ = catalog.carry_over_ids(
        fresh, pd.DataFrame(), frame(("ep-old", "2026-06-08T10:00:00")), LOG
    )
    assert (cat["event_id"].tolist() == ["ep-old"]) == kept


def test_matching_is_one_to_one_closest_first():
    fresh = frame(("ep-a", "2026-06-08T10:00:06"), ("ep-b", "2026-06-08T10:00:01"))
    previous = frame(("ep-old", "2026-06-08T10:00:00"))
    cat, _ = catalog.carry_over_ids(fresh, pd.DataFrame(), previous, LOG)
    assert cat["event_id"].tolist() == ["ep-a", "ep-old"]


def test_ids_are_only_carried_within_a_source():
    fresh = frame(("po-fresh", "2026-06-08T10:00:00"))
    cat, _ = catalog.carry_over_ids(
        fresh, pd.DataFrame(), frame(("ep-old", "2026-06-08T10:00:00")), LOG
    )
    assert cat["event_id"].tolist() == ["po-fresh"]


def test_nothing_previous_changes_nothing():
    fresh = frame(("ep-a", "2026-06-08T10:00:00"))
    assert catalog.carry_over_ids(fresh, pd.DataFrame(), pd.DataFrame(), LOG)[0].equals(fresh)


# ── through catalog.main(): incremental runs and rebuilds ─────────────


def write(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def day_files(out: Path, *events: tuple[int, str], jday: int = 159) -> None:
    """Write 2026/<jday> (default 2026-06-08) association output: (event_index, origin)."""
    base = out / f"4-events/2026/{jday}"
    write(base / f"2026.{jday}.events.csv", EVENT_COLS,
          *[f"{i},{t},1.0,31.8,-106.4,1.0,4" for i, t in events])  # fmt: skip
    write(base / f"2026.{jday}.assignments.csv", ASSIGN_COLS,
          *[f"{i},AM,KIDD,00,EHZ,P,{t},0.7,1e-6,EHZ" for i, t in events])  # fmt: skip


@pytest.fixture
def tree(tmp_path, sample_stations_json, monkeypatch):
    out = tmp_path / "output"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "stations_file": str(sample_stations_json),
                "input_dir": str(out),
                "output_dir": str(out),
                "log_dir": str(tmp_path / "logs"),
                "triage": {"enabled": False},
            }
        )
    )

    def run(*extra: str) -> pd.DataFrame:
        monkeypatch.setattr(sys, "argv", ["catalog.py", "--config", str(cfg), *extra])
        catalog.main()
        return pd.read_csv(out / "5-catalog/catalog.csv", dtype={"event_id": str})

    return out, run


def test_new_day_gets_time_based_ids(tree):
    out, run = tree
    day_files(out, (0, "2026-06-08T10:00:00.400"), (1, "2026-06-08T21:14:34.216"))
    assert run()["event_id"].tolist() == ["ep20260608T100000", "ep20260608T211434"]


def test_reassociation_keeps_every_id_and_review(tree):
    out, run = tree
    day_files(out, (0, "2026-06-08T10:00:00.400"), (1, "2026-06-08T21:14:34.216"))
    cat = run()
    cat.loc[cat["event_id"] == "ep20260608T211434", "review_status"] = "confirmed"
    cat.to_csv(out / "5-catalog/catalog.csv", index=False)
    # Re-association: GaMMA renumbers, origins move a few seconds, one event
    # is no longer found, a new one appears.
    day_files(out, (0, "2026-06-08T08:30:00.000"), (1, "2026-06-08T21:14:31.900"),
              (2, "2026-06-08T10:00:03.100"))  # fmt: skip
    cat = run("--rebuild").set_index("event_id")
    assert sorted(cat.index) == ["ep20260608T083000", "ep20260608T100000", "ep20260608T211434"]
    assert cat.loc["ep20260608T211434", "review_status"] == "confirmed"
    assert cat.loc["ep20260608T100000", "time"] == "2026-06-08T10:00:03.100"  # fresh solution
    asg = pd.read_csv(out / "5-catalog/assignments.csv", dtype={"event_id": str})
    assert set(asg["event_id"]) == set(cat.index)
    # Rebuilding again changes nothing.
    assert sorted(run("--rebuild")["event_id"]) == sorted(cat.index)


def test_legacy_positional_ids_survive_a_rebuild(tree):
    out, run = tree
    day_files(out, (0, "2026-06-08T10:00:00.400"))
    write(out / "5-catalog/catalog.csv", "event_id,event_index,time,review_status",
          "ep20260608-0000,0,2026-06-08T10:00:00.400,")  # fmt: skip
    assert run("--rebuild")["event_id"].tolist() == ["ep20260608-0000"]


# ── which days are ingested: the ledger, not the ids ──────────────────


def ledger(out: Path) -> dict:
    return json.loads((out / "5-catalog/ingested_days.json").read_text())


def test_event_just_before_midnight_is_not_ingested_twice(tree):
    out, run = tree
    day_files(out, (0, "2026-06-07T23:59:50.100"))  # in 2026-06-08's file
    assert run()["event_id"].tolist() == ["ep20260607T235950"]
    assert run()["event_id"].tolist() == ["ep20260607T235950"]
    assert ledger(out)["ep"] == ["20260608"]


def test_event_just_after_midnight_does_not_hide_the_next_day(tree):
    out, run = tree
    day_files(out, (0, "2026-06-09T00:00:03.000"))  # in 2026-06-08's file
    run()
    day_files(out, (0, "2026-06-09T10:00:00.000"), jday=160)
    assert run()["event_id"].tolist() == ["ep20260609T000003", "ep20260609T100000"]


def test_catalog_without_a_ledger_takes_its_days_from_positional_ids(tree):
    out, run = tree
    day_files(out, (0, "2026-06-08T10:00:00.400"))
    write(out / "5-catalog/catalog.csv", "event_id,event_index,time,review_status",
          "ep20260608-0000,0,2026-06-08T10:00:00.400,")  # fmt: skip
    assert run()["event_id"].tolist() == ["ep20260608-0000"]
    assert ledger(out) == {"ep": ["20260608"], "po": []}


def test_rebuild_rewrites_the_ledger(tree):
    out, run = tree
    day_files(out, (0, "2026-06-08T10:00:00.400"))
    run().assign(review_status="").to_csv(out / "5-catalog/catalog.csv", index=False)
    (out / "5-catalog/ingested_days.json").write_text('{"ep": ["20990101"]}')
    run("--rebuild")
    assert ledger(out)["ep"] == ["20260608"]
