"""Which association days the catalog step has ingested, per source.

``output/5-catalog/ingested_days.json`` holds {source: [YYYYMMDD, ...]}, with
source "ep" for PhaseNet's 4-events and "po" for PhaseNO's additions. The
catalog step skips a day already listed for that source.

This used to be read off the event ids, whose characters 3-10 were the day
file an event came from. Time-based ids carry the origin date instead, and
an association day can hold an event a few seconds either side of midnight,
so the id no longer says which day file was ingested. A catalog without the
ledger, i.e. one from before it existed, is bootstrapped from its ids
(``days_from_ids``), which is exact for positional ids.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from lib.atomic_io import atomic_write_text

INGESTED_DAYS_FILENAME = "ingested_days.json"
PRIMARY_SOURCE = "ep"
PHASENO_SOURCE = "po"


def days_from_ids(event_ids: Iterable[str]) -> dict[str, set[str]]:
    """Ledger implied by positional ids: PhaseNO ids -> "po", every other id -> "ep"."""
    days: dict[str, set[str]] = {PRIMARY_SOURCE: set(), PHASENO_SOURCE: set()}
    for eid in event_ids:
        source = PHASENO_SOURCE if eid.startswith(PHASENO_SOURCE) else PRIMARY_SOURCE
        days[source].add(eid[2:10])
    return days


def read_ingested_days(path: Path) -> dict[str, set[str]] | None:
    """The ledger, or None if there is none yet."""
    path = Path(path)
    if not path.exists():
        return None
    return {source: set(days) for source, days in json.loads(path.read_text()).items()}


def write_ingested_days(path: Path, days: dict[str, set[str]]) -> None:
    atomic_write_text(
        json.dumps({s: sorted(d) for s, d in sorted(days.items())}, indent=1) + "\n", Path(path)
    )
