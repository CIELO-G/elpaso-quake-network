"""5-catalog/catalog.py: PhaseNO additions as a second, per-day source ("po" ids)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "5-catalog"))
import catalog  # noqa: E402

EVENT_COLS = "event_index,time,magnitude,latitude,longitude,depth_km,num_picks"
ASSIGN_COLS = "event_index,network,station,location,channel,phase,time,probability,amplitude,amplitude_channel"


def write(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def picks(index: int, t: str) -> list[str]:
    return [
        f"{index},AM,{s},00,EHZ,P,{t},0.7,1e-6,EHZ" for s in ["KIDD", "R0F2D", "R4B41", "RE2E7"]
    ]


@pytest.fixture
def tree(tmp_path, sample_stations_json):
    out = tmp_path / "output"
    write(
        out / "4-events/2026/159/2026.159.events.csv",
        EVENT_COLS,
        "0,2026-06-08T10:00:00.000,1.0,31.8,-106.4,1.0,4",
    )
    write(
        out / "4-events/2026/159/2026.159.assignments.csv",
        ASSIGN_COLS,
        *picks(0, "2026-06-08T10:00:05.000"),
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "stations_file": str(sample_stations_json),
                "input_dir": str(out),
                "output_dir": str(out),
                "log_dir": str(tmp_path / "logs"),
                "triage": {"enabled": True},
            }
        )
    )
    return out, cfg


def add_phaseno(out: Path) -> None:
    base = out / "phaseno/4-additions/2026/159"
    write(
        base / "2026.159.events.csv",
        EVENT_COLS + ",corroborating_picks",
        "3,2026-06-08T21:14:34.216,1.2,31.9,-106.3,1.0,4,3",
    )
    write(base / "2026.159.assignments.csv", ASSIGN_COLS, *picks(3, "2026-06-08T21:14:40.000"))


def run(monkeypatch, cfg: Path, *extra: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    monkeypatch.setattr(sys, "argv", ["catalog.py", "--config", str(cfg), *extra])
    catalog.main()
    out = Path(yaml.safe_load(cfg.read_text())["output_dir"]) / "5-catalog"
    return (
        pd.read_csv(out / "catalog.csv", dtype={"event_id": str}),
        pd.read_csv(out / "assignments.csv", dtype={"event_id": str}),
    )


def test_additions_get_po_ids_detector_and_their_picks(tree, monkeypatch):
    out, cfg = tree
    add_phaseno(out)
    cat, asg = run(monkeypatch, cfg)
    assert cat["event_id"].tolist() == ["ep20260608T100000", "po20260608T211434"]
    po = cat.set_index("event_id").loc["po20260608T211434"]
    assert po["detector"] == "PhaseNO" and po["corroborating_picks"] == 3
    assert pd.isna(cat.set_index("event_id").loc["ep20260608T100000", "detector"])
    assert (asg["event_id"] == "po20260608T211434").sum() == 4
    assert po["triage"] in ("ok", "flag")  # triage labels the additions too


def test_additions_that_arrive_later_are_ingested_once(tree, monkeypatch):
    out, cfg = tree
    cat, _ = run(monkeypatch, cfg)
    assert cat["event_id"].tolist() == ["ep20260608T100000"]
    add_phaseno(out)  # e.g. a backfill or a retried PhaseNO run
    cat, asg = run(monkeypatch, cfg)
    assert cat["event_id"].tolist() == ["ep20260608T100000", "po20260608T211434"]
    cat, asg = run(monkeypatch, cfg)
    assert len(cat) == 2 and len(asg) == 8


def test_rebuild_keeps_a_reviewed_addition(tree, monkeypatch):
    out, cfg = tree
    add_phaseno(out)
    cat, _ = run(monkeypatch, cfg)
    cat.loc[cat["event_id"] == "po20260608T211434", "review_status"] = "confirmed"
    cat.to_csv(out / "5-catalog/catalog.csv", index=False)
    cat, _ = run(monkeypatch, cfg, "--rebuild")
    po = cat[cat["event_id"] == "po20260608T211434"]
    assert len(cat) == 2 and po["review_status"].tolist() == ["confirmed"]


def test_additions_source_can_be_switched_off(tree, monkeypatch):
    out, cfg = tree
    add_phaseno(out)
    c = yaml.safe_load(cfg.read_text())
    c["phaseno_additions_dir"] = None
    cfg.write_text(yaml.safe_dump(c))
    cat, _ = run(monkeypatch, cfg)
    assert cat["event_id"].tolist() == ["ep20260608T100000"]


def test_cataloged_days_split_by_source():
    df = pd.DataFrame({"event_id": ["ep20260608T100000", "tm20260609-0000", "po20260610-0003"]})
    assert catalog.cataloged_days(df) == {"20260608", "20260609"}
    assert catalog.cataloged_days(df, catalog.PHASENO_PREFIX) == {"20260610"}
    assert catalog.cataloged_days(pd.DataFrame()) == set()
