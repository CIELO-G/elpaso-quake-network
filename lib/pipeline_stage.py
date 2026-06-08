"""Shared helpers for pipeline stage scripts (process / detect / associate).

Each stage script wraps a stdlib-argparse CLI, resolves a UTC time window
from ``--start`` / ``--end`` (or falls back to scheduled-mode defaults), and
walks the window day by day. The two patterns below were copy-pasted across
three modules; centralising them avoids subtle drift (the midnight-end-time
trick in particular is easy to get wrong).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

from obspy import UTCDateTime


def resolve_time_window(
    args,
    config: dict,
    *,
    window_hours_key: str,
    latency_hours_key: str,
    logger,
) -> tuple[UTCDateTime, UTCDateTime]:
    """Return ``(start_time, end_time)`` from CLI args + config.

    Behaviour:
      * Both ``--start`` and ``--end`` provided: use them verbatim
        ("Explicit range").
      * Neither provided: scheduled mode — ``end = now - latency``,
        ``start = end - window``.
      * Only one provided: log an error and ``sys.exit(1)``.

    Parameters
    ----------
    args
        Parsed CLI namespace; must expose ``.start`` and ``.end``.
    config
        Stage config dict; must contain ``window_hours_key`` and
        ``latency_hours_key`` (e.g. ``"process_window_hours"`` /
        ``"process_latency_hours"``).
    logger
        Logger used to announce the resolved mode.
    """
    if args.start and args.end:
        start_time = UTCDateTime(args.start)
        end_time = UTCDateTime(args.end)
        logger.info("Explicit range: %s -> %s", start_time, end_time)
    elif args.start or args.end:
        logger.error("Both --start and --end are required together")
        sys.exit(1)
    else:
        end_time = UTCDateTime() - config[latency_hours_key] * 3600
        start_time = end_time - config[window_hours_key] * 3600
        logger.info("Scheduled mode: %s -> %s", start_time, end_time)
    return start_time, end_time


def iter_days(
    start_time: UTCDateTime,
    end_time: UTCDateTime,
) -> Iterator[tuple[str, str, UTCDateTime]]:
    """Yield ``(year_str, jday_str, day_start_utc)`` for each calendar day in
    the half-open window ``[start_time, end_time)``.

    A midnight ``end_time`` is treated as belonging to the previous day, so
    a window like ``2026-01-29 → 2026-01-30`` yields one day, not two. Without
    this guard the loop would emit a phantom day every time the orchestrator
    passes ``end = start + 1 day`` (which it always does in continuous mode).
    """
    day = UTCDateTime(start_time.year, start_time.month, start_time.day)
    end_adj = end_time - 1
    end_day = UTCDateTime(end_adj.year, end_adj.month, end_adj.day)
    while day <= end_day:
        yield str(day.year), f"{day.julday:03d}", day
        day += 86400
