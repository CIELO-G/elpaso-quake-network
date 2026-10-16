"""Event ids: {prefix}{YYYYMMDD}T{HHMMSS}.

The two-character source prefix (ep PhaseNet, po PhaseNO, tm template
matching) and the origin time of the event's FIRST solution, to the second.
An id is issued once and never changes; the catalog step carries ids across
rebuilds (5-catalog/catalog.py carry_over_ids). In every form characters
3-10 are the UTC day.

Ids issued before 2026-10-16 were positional, {prefix}{YYYYMMDD}-{NNNN}.
scripts/migrate_event_ids.py renamed them; output/5-catalog/id_aliases.csv
maps each old id to its new one so old notes, links and review history
still resolve.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

ID_TIME_FORMAT = "%Y%m%dT%H%M%S"
LEGACY_ID = re.compile(r"^[a-z]{2}\d{8}-\d{4}$")
ALIASES_FILENAME = "id_aliases.csv"
ALIAS_COLUMNS = ["legacy_id", "event_id"]


def mint_event_id(origin: str | datetime, prefix: str, taken: set[str]) -> str:
    """New id for an event with this origin (UTC; naive = UTC); "-2", "-3"... if taken.

    Adds the id to `taken`.
    """
    if len(prefix) != 2:
        raise ValueError(f"event id prefix must be 2 characters, got {prefix!r}")
    t = (
        origin
        if isinstance(origin, datetime)
        else datetime.fromisoformat(str(origin).replace("Z", "+00:00"))
    )
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc)
    base = f"{prefix}{t.strftime(ID_TIME_FORMAT)}"
    eid, n = base, 1
    while eid in taken:
        n += 1
        eid = f"{base}-{n}"
    taken.add(eid)
    return eid


def is_legacy_id(event_id: str) -> bool:
    """True for a positional id, {prefix}{YYYYMMDD}-{NNNN}."""
    return bool(LEGACY_ID.match(event_id))


def read_aliases(path: Path) -> dict[str, str]:
    """legacy id -> current id from an id_aliases.csv (empty if missing)."""
    if not Path(path).exists():
        return {}
    with open(path, newline="") as f:
        return {r["legacy_id"]: r["event_id"] for r in csv.DictReader(f)}
