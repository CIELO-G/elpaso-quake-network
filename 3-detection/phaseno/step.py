#!/usr/bin/env python3
"""PhaseNO as a second detector: pipeline step between association and catalog.

For each day:

  1. pick with PhaseNO (predict.py, in the `phaseno` conda env, from 1-raw);
  2. associate those picks with the production association settings
     (4-association/associate.py with its I/O pointed at `output_dir`);
  3. select the PhaseNO events PhaseNet missed (lib/detector_merge.py) and
     write them to {output_dir}/4-additions/, where the catalog step picks
     them up with event-id prefix "po".

PhaseNet's own files are only read. Outputs, under `output_dir`:

    3-picks/Y/J/Y.J.picks.csv             PhaseNO picks
    4-events/Y/J/Y.J.{events,assignments}.csv   PhaseNO events (all)
    4-additions/Y/J/Y.J.{events,assignments}.csv  events the merge adds

Steps 1 and 2 skip days that already have output unless --force; step 3 is
cheap and always recomputed. Exit 1 when any day failed; the orchestrator
treats this step as optional (alert, day still completes). `enabled` in the
config only tells the orchestrator whether to run it; running this script by
hand (e.g. to backfill) works either way.

Usage (project root, elpaso-quake env):
    python 3-detection/phaseno/step.py --config 3-detection/phaseno/config.yaml \\
        --start 2026-10-01 --end 2026-10-02
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.atomic_io import atomic_write_df  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.detector_merge import MergeConfig, select_additions  # noqa: E402
from lib.logger import setup_logging  # noqa: E402

DEFAULTS = {
    "enabled": False,
    "stations_file": "stations.json",
    "python": "/opt/anaconda3/envs/phaseno/bin/python",
    "device": "auto",
    "p_threshold": 0.4,
    "s_threshold": 0.4,
    "raw_dir": "output/1-raw",
    "primary_dir": "output",
    "output_dir": "output/phaseno",
    "association_config": "4-association/config.yaml",
    "merge": {},
    "log_dir": "logs",
    "log_max_bytes": 10_485_760,
    "log_backup_count": 5,
}

PREDICT = ROOT / "3-detection" / "phaseno" / "predict.py"
ASSOCIATE = ROOT / "4-association" / "associate.py"
CSV_DTYPES = {"network": str, "station": str, "location": str, "channel": str}

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class DayFailed(Exception):
    """One day could not be completed; the message says which part and why."""


def _run_captured(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)


@dataclass(frozen=True)
class DayPaths:
    year: int
    jday: int
    raw_dir: Path
    primary_dir: Path
    output_dir: Path

    def _daily(self, base: Path, sub: str, kind: str) -> Path:
        y, j = f"{self.year}", f"{self.jday:03d}"
        return base / sub / y / j / f"{y}.{j}.{kind}.csv"

    @property
    def raw_day(self) -> Path:
        return self.raw_dir / f"{self.year}" / f"{self.jday:03d}"

    @property
    def picks(self) -> Path:
        return self._daily(self.output_dir, "3-picks", "picks")

    def events(self, kind: str = "events") -> Path:
        return self._daily(self.output_dir, "4-events", kind)

    def additions(self, kind: str = "events") -> Path:
        return self._daily(self.output_dir, "4-additions", kind)

    @property
    def primary_picks(self) -> Path:
        return self._daily(self.primary_dir, "3-picks", "picks")

    def primary_events(self, kind: str = "events") -> Path:
        return self._daily(self.primary_dir, "4-events", kind)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=CSV_DTYPES, keep_default_na=False)


def _check(result: subprocess.CompletedProcess, what: str, logger) -> None:
    for line in (result.stdout + result.stderr).splitlines():
        logger.debug("  [%s] %s", what, line)
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-5:]
        raise DayFailed(f"{what} exited {result.returncode}: " + " | ".join(tail))


def run_day(
    day: date, config: dict, *, force: bool, debug: bool, logger, run: Runner = _run_captured
) -> int:
    """Pick, associate and merge one day. Returns the number of events added."""
    jday = day.timetuple().tm_yday
    p = DayPaths(
        day.year,
        jday,
        Path(config["raw_dir"]),
        Path(config["primary_dir"]),
        Path(config["output_dir"]),
    )
    if not p.primary_events().exists():
        raise DayFailed(f"no PhaseNet events file {p.primary_events()} (run association first)")

    if force or not p.picks.exists():
        cmd = [
            config["python"], str(PREDICT),
            "--day-dir", str(p.raw_day),
            "--stations", str(config["stations_file"]),
            "--input-units", "counts",
            "--out", str(p.picks),
            "--p-threshold", str(config["p_threshold"]),
            "--s-threshold", str(config["s_threshold"]),
            "--device", str(config["device"]),
        ]  # fmt: skip
        if force:
            cmd.append("--force")
        if debug:
            cmd.append("--debug")
        _check(run(cmd), "predict", logger)

    if force or not p.events().exists():
        cmd = [
            sys.executable, "-u", str(ASSOCIATE),
            "--config", str(config["association_config"]),
            "--start", day.isoformat(),
            "--end", (day + timedelta(days=1)).isoformat(),
            "--input-dir", str(p.output_dir),
            "--output-dir", str(p.output_dir),
            "--log-name", "associate_phaseno",
        ]  # fmt: skip
        if force:
            cmd.append("--force")
        if debug:
            cmd.append("--debug")
        _check(run(cmd), "associate", logger)
    if not p.events().exists():
        raise DayFailed(f"association wrote no {p.events()}")

    primary_picks = _read(p.primary_picks) if p.primary_picks.exists() else pd.DataFrame()
    secondary_events = _read(p.events())
    assignments_path = p.events("assignments")
    secondary_assignments = _read(assignments_path) if assignments_path.exists() else pd.DataFrame()
    added, added_picks = select_additions(
        _read(p.primary_events()),
        primary_picks,
        secondary_events,
        secondary_assignments,
        MergeConfig.from_config(config["merge"]),
    )
    # Assignments first: the events file marks the day as done (as in associate.py).
    atomic_write_df(added_picks, p.additions("assignments"), index=False)
    atomic_write_df(added, p.additions(), index=False)
    logger.info(
        "%s: %d PhaseNO event(s), %d added%s",
        day,
        len(secondary_events),
        len(added),
        "".join(
            f"\n    + {r['time']}  corroborating picks {r['corroborating_picks']}"
            for r in added.to_dict("records")
        ),
    )
    return len(added)


def _days(start: str, end: str) -> list[date]:
    """Days in [start, end); a single date means that one day."""
    first, last = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    if last <= first:
        last = first + timedelta(days=1)
    return [first + timedelta(days=i) for i in range((last - first).days)]


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="PhaseNO second-detector step")
    ap.add_argument("--config", required=True, help="Path to YAML configuration file")
    ap.add_argument("--start", required=True, help="First day (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="Day after the last (exclusive)")
    ap.add_argument("--force", action="store_true", help="Re-pick and re-associate")
    ap.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(args.config, defaults=DEFAULTS)
    logger = setup_logging("phaseno", config, debug=args.debug)
    days = _days(args.start, args.end)
    logger.info("PhaseNO step: %s .. %s (%d day(s))", days[0], days[-1], len(days))
    failed = []
    for day in days:
        try:
            run_day(day, config, force=args.force, debug=args.debug, logger=logger)
        except DayFailed as exc:
            logger.error("%s: %s", day, exc)
            failed.append(day.isoformat())
    if failed:
        logger.error("PhaseNO failed for %d day(s): %s", len(failed), ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
