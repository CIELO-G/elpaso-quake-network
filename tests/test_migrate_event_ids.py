"""scripts/migrate_event_ids.py: positional ids -> time-based ids, once."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "migrate_event_ids", ROOT / "scripts/migrate_event_ids.py"
)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

sys.path.insert(0, str(ROOT / "5-catalog"))
import catalog  # noqa: E402

CATALOG = """event_id,event_index,time,magnitude,review_status,triage_reason
ep20260608-0001,1,2026-06-08T21:14:34.216,1.2,confirmed,"a, b"
ep20260608-0000,0,2026-06-08T10:00:00.400,,,
ep20260609-0000,0,2026-06-08T23:59:50.100,0.8,,
tm20260608-0000,,2026-06-08T21:14:34.900,0.5,rejected,
"""
ASSIGNMENTS = """event_id,station,time
ep20260608-0000,KIDD,2026-06-08T10:00:05.000
ep20260608-0001,KIDD,2026-06-08T21:14:40.000
ep20260608-0001,R0F2D,2026-06-08T21:14:41.000
"""


def t(s: str) -> datetime:
    return mig.utc(s)


def rows(*items: tuple[str, str]) -> list[dict]:
    return [{"event_id": i, "time": tm} for i, tm in items]


# ── planning ──────────────────────────────────────────────────────────


def test_ids_come_from_origin_times_with_prefix_and_day_of_origin():
    plan = mig.plan_event_ids(
        rows(
            ("ep20260608-0001", "2026-06-08T21:14:34.216"),
            ("ep20260609-0000", "2026-06-08T23:59:50.1Z"),
            ("tm20260608-0000", "2026-06-08T21:14:34.9"),
        )
    )
    assert plan == {
        "ep20260608-0001": "ep20260608T211434",
        "ep20260609-0000": "ep20260608T235950",
        "tm20260608-0000": "tm20260608T211434",
    }


def test_same_second_goes_to_the_earlier_event_and_new_ids_are_kept():
    plan = mig.plan_event_ids(
        rows(
            ("ep20260608-0003", "2026-06-08T10:00:00.9"),
            ("ep20260608-0002", "2026-06-08T10:00:00.1"),
            ("ep20260608T120000", "2026-06-08T12:00:00.5"),
            ("ep20260608-0004", "2026-06-08T12:00:00.0"),
        )
    )
    assert plan == {
        "ep20260608-0002": "ep20260608T100000",
        "ep20260608-0003": "ep20260608T100000-2",
        "ep20260608-0004": "ep20260608T120000-2",
    }


def test_duplicate_ids_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        mig.plan_event_ids(
            rows(
                ("ep20260608-0000", "2026-06-08T10:00:00"),
            )
            * 2
        )


def test_templates_follow_their_event_by_origin_time():
    events = [("ep20260608T100000", t("2026-06-08T10:00:00.4")),
              ("tm20260608T100001", t("2026-06-08T10:00:01.0"))]  # fmt: skip
    aliases = {"ep20260608-0000": "ep20260608T100000"}
    names = mig.plan_template_names(
        # -0003 drifted: it is a second template of the same event.
        [
            ("ep20260608-0003", t("2026-06-08T10:00:00.2")),
            ("ep20260608-0000", t("2026-06-08T10:00:01.9")),
        ],  # fmt: skip
        events,
        aliases,
    )
    assert names == {
        "ep20260608-0000": "ep20260608T100000",
        "ep20260608-0003": "ep20260608T100000_2",
    }


def test_a_template_without_its_event_is_an_error():
    with pytest.raises(ValueError, match="ep20260608-0000"):
        mig.plan_template_names(
            [("ep20260608-0000", t("2026-06-08T10:00:00"))],
            [("ep20260608T100030", t("2026-06-08T10:00:30"))],
            {},
        )


# ── applying ──────────────────────────────────────────────────────────


@pytest.fixture
def output(tmp_path):
    out = tmp_path / "output"
    cat_dir = out / "5-catalog"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalog.csv").write_text(CATALOG)
    (cat_dir / "assignments.csv").write_text(ASSIGNMENTS)
    (cat_dir / "reviews.jsonl").write_text(
        '{"event_id": "ep20260608-0001", "event": {}, "picks": []}\n'
        '{"event_id": "tm20260608-0000", "event": {}, "picks": []}\n'
    )
    return out


def files(d: Path) -> dict[Path, bytes]:
    """Contents of the data files in `d` (not the lock file, not backups)."""
    return {p: p.read_bytes() for p in d.iterdir() if p.is_file() and not p.name.startswith(".")}


def test_dry_run_writes_nothing(output):
    before = files(output / "5-catalog")
    assert mig.main(["--output-dir", str(output)]) == 0
    assert files(output / "5-catalog") == before


def test_apply_renames_ids_and_nothing_else(output):
    cat_dir = output / "5-catalog"
    old_reviews = (cat_dir / "reviews.jsonl").read_text()
    mig.main(["--output-dir", str(output), "--apply"])

    aliases = mig.read_aliases(cat_dir / "id_aliases.csv")
    assert aliases["ep20260609-0000"] == "ep20260608T235950"
    restored = (cat_dir / "catalog.csv").read_text()
    for old, new in aliases.items():
        restored = restored.replace(new + ",", old + ",")
    assert restored == CATALOG  # byte for byte, quoting and empty cells included

    picks = pd.read_csv(cat_dir / "assignments.csv")
    assert picks["event_id"].tolist() == ["ep20260608T100000", "ep20260608T211434",
                                          "ep20260608T211434"]  # fmt: skip

    reviews = (cat_dir / "reviews.jsonl").read_text()
    assert reviews.startswith(old_reviews)
    added = [json.loads(line) for line in reviews[len(old_reviews) :].splitlines()]
    assert {
        (r["source"], r["event_id"], r["event"]["legacy_id"], len(r["picks"])) for r in added
    } == {
        ("id_migration", "ep20260608T211434", "ep20260608-0001", 2),
        ("id_migration", "tm20260608T211434", "tm20260608-0000", 0),
    }

    # The ledger keeps the day files the events came from, not origin dates.
    days = json.loads((cat_dir / "ingested_days.json").read_text())
    assert days == {"ep": ["20260608", "20260609"], "po": []}
    assert len(list(cat_dir.glob("id-migration-backup-*/*"))) == 3

    after = files(cat_dir)
    mig.main(["--output-dir", str(output), "--apply"])  # second run: no-op
    assert files(cat_dir) == after


def test_catalog_step_after_migration_ingests_nothing_again(output, tmp_path, sample_stations_json,
                                                            monkeypatch):  # fmt: skip
    for jday, origin in ((159, "2026-06-08T10:00:00.400"), (160, "2026-06-08T23:59:50.100")):
        base = output / f"4-events/2026/{jday}"
        base.mkdir(parents=True)
        (base / f"2026.{jday}.events.csv").write_text(
            f"event_index,time,magnitude,latitude,longitude,depth_km,num_picks\n"
            f"0,{origin},1.0,31.8,-106.4,1.0,4\n"
        )
    mig.main(["--output-dir", str(output), "--apply"])
    before = (output / "5-catalog/catalog.csv").read_text()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "stations_file": str(sample_stations_json), "input_dir": str(output),
        "output_dir": str(output), "log_dir": str(tmp_path / "logs"),
        "triage": {"enabled": False},
    }))  # fmt: skip
    monkeypatch.setattr(sys, "argv", ["catalog.py", "--config", str(cfg)])
    catalog.main()
    assert (output / "5-catalog/catalog.csv").read_text() == before


def test_utc_treats_naive_as_utc():
    assert mig.utc("2026-06-08T10:00:00") == datetime(2026, 6, 8, 10, tzinfo=timezone.utc)
