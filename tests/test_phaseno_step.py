"""3-detection/phaseno/step.py: per-day orchestration of pick -> associate -> merge.

predict.py and associate.py are replaced by a fake runner that writes the
files they would; the real merge rule (lib/detector_merge.py) runs.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("phaseno_step", ROOT / "3-detection/phaseno/step.py")
step = importlib.util.module_from_spec(_spec)
sys.modules["phaseno_step"] = step
_spec.loader.exec_module(step)

DAY = date(2026, 6, 8)  # 2026/159
LOG = logging.getLogger("test_phaseno_step")
PICK_COLS = (
    "network,station,location,channel,phase,time,probability,model,amplitude,amplitude_channel"
)
ASSIGN_COLS = "event_index,network,station,location,channel,phase,time,probability,amplitude,amplitude_channel"
EVENT_COLS = "event_index,time,magnitude,num_picks"


def write(path: Path, header: str, *rows: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header, *rows]) + "\n")


@pytest.fixture
def cfg(tmp_path):
    out = tmp_path / "output"
    write(
        out / "4-events/2026/159/2026.159.events.csv", EVENT_COLS, "0,2026-06-08T10:00:00.000,1.0,6"
    )
    write(
        out / "3-picks/2026/159/2026.159.picks.csv",
        PICK_COLS,
        *[f"AM,{s},00,EHZ,P,2026-06-08T21:14:4{k}.000,0.6,PhaseNet,1e-6,EHZ"
          for k, s in enumerate(["KIDD", "R0F2D", "R4B41"])],
    )  # fmt: skip
    return {
        **step.DEFAULTS,
        "raw_dir": str(out / "1-raw"),
        "primary_dir": str(out),
        "output_dir": str(out / "phaseno"),
    }


class FakeTools:
    """Stands in for predict.py and associate.py; records the commands."""

    def __init__(self, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        tool = "predict" if cmd[1].endswith("predict.py") else "associate"
        if tool == self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "simulated failure")
        if tool == "predict":
            out = Path(cmd[cmd.index("--out") + 1])
            write(out, PICK_COLS)
        else:
            base = Path(cmd[cmd.index("--output-dir") + 1]) / "4-events/2026/159"
            # Event 3 was missed by PhaseNet and has its 3 picks; event 4 has none.
            write(
                base / "2026.159.events.csv",
                EVENT_COLS,
                "3,2026-06-08T21:14:34.216,1.2,4",
                "4,2026-06-08T23:00:00.000,0.9,4",
            )
            write(
                base / "2026.159.assignments.csv",
                ASSIGN_COLS,
                *[f"3,AM,{s},00,EHZ,P,2026-06-08T21:14:4{k}.300,0.7,1e-6,EHZ"
                  for k, s in enumerate(["KIDD", "R0F2D", "R4B41", "RE2E7"])],
                "4,AM,KIDD,00,EHZ,P,2026-06-08T23:00:05.000,0.7,1e-6,EHZ",
            )  # fmt: skip
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    def tools(self) -> list[str]:
        return ["predict" if c[1].endswith("predict.py") else "associate" for c in self.calls]


def additions(cfg, kind="events") -> pd.DataFrame:
    return pd.read_csv(Path(cfg["output_dir"]) / f"4-additions/2026/159/2026.159.{kind}.csv")


def test_day_picks_associates_and_writes_the_additions(cfg):
    fake = FakeTools()
    n = step.run_day(DAY, cfg, force=False, debug=False, logger=LOG, run=fake)
    assert n == 1 and fake.tools() == ["predict", "associate"]
    predict, associate = fake.calls
    assert predict[0] == cfg["python"] and "--input-units" in predict
    assert predict[predict.index("--day-dir") + 1].endswith("1-raw/2026/159")
    assert associate[associate.index("--input-dir") + 1] == cfg["output_dir"]
    assert associate[associate.index("--log-name") + 1] == "associate_phaseno"
    assert associate[associate.index("--start") + 1 :][:3] == ["2026-06-08", "--end", "2026-06-09"]
    ev = additions(cfg)
    assert ev["event_index"].tolist() == [3] and ev["corroborating_picks"].tolist() == [3]
    assert set(additions(cfg, "assignments")["event_index"]) == {3}


def test_existing_outputs_are_reused_unless_forced(cfg):
    step.run_day(DAY, cfg, force=False, debug=False, logger=LOG, run=FakeTools())
    again = FakeTools()
    assert step.run_day(DAY, cfg, force=False, debug=False, logger=LOG, run=again) == 1
    assert again.calls == []  # merge recomputed from the files on disk
    forced = FakeTools()
    step.run_day(DAY, cfg, force=True, debug=False, logger=LOG, run=forced)
    assert forced.tools() == ["predict", "associate"]
    assert all("--force" in c for c in forced.calls)


@pytest.mark.parametrize("tool", ["predict", "associate"])
def test_a_failing_tool_fails_the_day_without_additions(cfg, tool):
    with pytest.raises(step.DayFailed, match=f"{tool} exited 1: simulated failure"):
        step.run_day(DAY, cfg, force=False, debug=False, logger=LOG, run=FakeTools(fail=tool))
    assert not (Path(cfg["output_dir"]) / "4-additions").exists()


def test_day_without_phasenet_events_is_refused_before_picking(cfg):
    Path(cfg["primary_dir"], "4-events/2026/159/2026.159.events.csv").unlink()
    fake = FakeTools()
    with pytest.raises(step.DayFailed, match="no PhaseNet events"):
        step.run_day(DAY, cfg, force=False, debug=False, logger=LOG, run=fake)
    assert fake.calls == []


def test_phasenet_event_nearby_blocks_the_addition(cfg):
    write(
        Path(cfg["primary_dir"]) / "4-events/2026/159/2026.159.events.csv",
        EVENT_COLS,
        "0,2026-06-08T21:14:30.000,1.0,6",
    )
    assert step.run_day(DAY, cfg, force=False, debug=False, logger=LOG, run=FakeTools()) == 0
    assert additions(cfg).empty and "corroborating_picks" in additions(cfg).columns


def test_main_exits_1_when_any_day_fails(tmp_path, monkeypatch):
    seen = []

    def fake_day(day, config, **kw):
        seen.append(day)
        if day == date(2026, 6, 9):
            raise step.DayFailed("boom")
        return 0

    monkeypatch.setattr(step, "run_day", fake_day)
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(f"log_dir: {tmp_path}/logs\n")
    args = ["--config", str(cfg_file), "--start", "2026-06-08", "--end", "2026-06-11"]
    assert step.main(args) == 1
    assert seen == [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    assert (
        step.main(["--config", str(cfg_file), "--start", "2026-06-10", "--end", "2026-06-11"]) == 0
    )


def test_shipped_config_is_off_and_matches_the_evaluated_rule():
    import yaml

    shipped = yaml.safe_load((ROOT / "3-detection/phaseno/config.yaml").read_text())
    assert shipped["enabled"] is False
    merge = step.MergeConfig.from_config(shipped["merge"])
    assert merge == step.MergeConfig(3, 1.0, 10.0)
