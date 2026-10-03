"""Append-only record of human review decisions (``reviews.jsonl``).

``catalog.csv`` is the only file that carries reviewed-event state, and it is
rewritten whole by several writers. This sidecar is the durable, append-only
history: one JSON line per save, carrying the full event row and its picks,
so any event can be reconstructed by taking its most recent record.

Writers: the dashboard on every save or status change, the catalog step on
``--rebuild``, and the one-off backfill script. The file is never read by the
pipeline and never rewritten.
"""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

SIDECAR_FILENAME = "reviews.jsonl"


def jsonable(row: dict) -> dict:
    """Convert a pandas/CSV row to plain JSON-safe values (NaN -> None)."""
    out: dict = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
        elif isinstance(v, float) and math.isnan(v):
            out[k] = None
        elif hasattr(v, "item"):  # numpy scalar
            out[k] = v.item()
        else:
            out[k] = v
    return out


def append_review_records(
    sidecar_path: Path,
    events: dict[str, dict],
    assignments: dict[str, list],
    *,
    source: str,
) -> int:
    """Append one record per event to ``sidecar_path``. Returns the count.

    ``events`` maps event_id -> full catalog row; ``assignments`` maps
    event_id -> list of pick rows. ``source`` says who wrote the record
    (``dashboard_save``, ``dashboard_status``, ``rebuild``, ``backfill``).
    """
    if not events:
        return 0
    sidecar_path = Path(sidecar_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    n = 0
    # Append-only: 'a' mode; never read or rewrite the file here.
    with sidecar_path.open("a", encoding="utf-8") as f:
        for eid, ev in events.items():
            record = {
                "timestamp": ts,
                "source": source,
                "event_id": eid,
                "event": jsonable(ev),
                "picks": [jsonable(p) for p in assignments.get(eid, [])],
            }
            f.write(json.dumps(record, default=str) + "\n")
            n += 1
        f.flush()
    return n


def latest_records(sidecar_path: Path) -> dict[str, dict]:
    """Most recent record per event_id (empty dict if the file is missing).

    Malformed lines are skipped rather than failing: the file is append-only
    and a torn last line after a crash must not make history unreadable.
    """
    sidecar_path = Path(sidecar_path)
    latest: dict[str, dict] = {}
    if not sidecar_path.exists():
        return latest
    with sidecar_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = rec.get("event_id")
            if eid:
                latest[eid] = rec
    return latest
