"""Event ids: {prefix}{YYYYMMDD}T{HHMMSS}.

The two-character source prefix (ep PhaseNet, po PhaseNO, tm template
matching) and the origin time of the event's FIRST solution, to the second.
An id is issued once and never changes; the catalog step carries ids across
rebuilds (5-catalog/catalog.py carry_over_ids). Ids issued before 2026-10-16
are positional, {prefix}{YYYYMMDD}-{NNNN}, and keep that form. In both forms
characters 3-10 are the UTC day.
"""

from __future__ import annotations

from datetime import datetime, timezone

ID_TIME_FORMAT = "%Y%m%dT%H%M%S"


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
