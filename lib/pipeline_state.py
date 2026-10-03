"""Persistent orchestrator state (survives restarts).

``output/pipeline_status.json`` is the live view the dashboard reads; it is
rebuilt from scratch every time the orchestrator starts. The lists that must
survive a restart — days that failed and were skipped, days whose ingest was
only partial — live here instead, in ``output/pipeline_state.json``.

Both lists hold ISO dates (``YYYY-MM-DD``), sorted, without duplicates.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lib.atomic_io import atomic_write_text

STATE_FILENAME = "pipeline_state.json"


@dataclass
class PipelineState:
    days_skipped: list[str] = field(default_factory=list)
    days_partial: list[str] = field(default_factory=list)
    dark_stations: list[str] = field(default_factory=list)  # NET.STA currently alerted as dark
    updated_at: str | None = None

    # ── mutation helpers (keep lists sorted + unique) ──
    def add_skipped(self, day: str) -> None:
        self.days_skipped = sorted(set(self.days_skipped) | {day})

    def remove_skipped(self, day: str) -> None:
        self.days_skipped = [d for d in self.days_skipped if d != day]

    def add_partial(self, day: str) -> None:
        self.days_partial = sorted(set(self.days_partial) | {day})

    def remove_partial(self, day: str) -> None:
        self.days_partial = [d for d in self.days_partial if d != day]


def state_path(output_dir: Path) -> Path:
    return Path(output_dir) / STATE_FILENAME


def load_state(output_dir: Path) -> PipelineState:
    """Read the state file; a missing or unreadable file yields empty state."""
    path = state_path(output_dir)
    if not path.exists():
        return PipelineState()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return PipelineState()
    st = PipelineState(
        days_skipped=sorted(set(raw.get("days_skipped", []))),
        days_partial=sorted(set(raw.get("days_partial", []))),
        dark_stations=sorted(set(raw.get("dark_stations", []))),
        updated_at=raw.get("updated_at"),
    )
    return st


def save_state(output_dir: Path, state: PipelineState) -> None:
    state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_write_text(json.dumps(asdict(state), indent=2) + "\n", state_path(output_dir))
