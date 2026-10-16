"""Regional-earthquake tagging for triage.

Regional earthquakes (mostly Delaware Basin, 150-350 km east) arrive across
the network as coherent P/S trains that GaMMA associates into a local event
with an origin 5-90 s after the real one. Measured on the reviewed catalog
(research/phaseno-eval, Round 2): an origin 5-90 s after a ComCat event of
M >= 2.0 more than 150 km away matched 10 of 50 reviewer-rejected events and
0 of 47 confirmed ones; chance collisions are ~0.4 % (about 4 such events a
day x 85 s window).

The shipped thresholds are deliberately stricter, M >= 3.0 at 150-350 km
(operator's choice, 2026-10-16): on the live catalog that tags 8 of 50
rejected and 0 of 46 confirmed, against 11 of 50 at M >= 2.0. Every match
seen so far was within 321 km, so the 350 km cap costs nothing.

This module owns the ComCat side: fetching, a local cache with explicit
coverage, and the time-window match. lib/triage.py turns a match into a label.

Network access is best effort. A failed fetch logs a warning and the cached
events are used; an event outside the cached coverage is never labelled, so
an outage can only miss tags, never invent them.
"""

from __future__ import annotations

import io
import json
import logging
import math
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

from lib.atomic_io import atomic_write_df, atomic_write_text

_default_log = logging.getLogger(__name__)

COMCAT_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
# USGS refuses queries that would return more than this many events.
COMCAT_MAX_EVENTS = 20000
EARTH_RADIUS_KM = 6371.0

CACHE_COLUMNS = ["id", "time", "latitude", "longitude", "mag", "place", "distance_km"]

# (start, end) -> CSV text of a USGS fdsnws/event query.
Fetcher = Callable[[pd.Timestamp, pd.Timestamp], str]


@dataclass(frozen=True)
class RegionalConfig:
    enabled: bool = False
    # Network reference point. Explicit rather than derived from
    # stations.json, so adding or retiring stations cannot move it.
    reference_lat: float = 31.778
    reference_lon: float = -106.408
    min_magnitude: float = 3.0
    min_distance_km: float = 150.0
    max_distance_km: float = 350.0
    # A local origin this many seconds after a regional event is tagged.
    lag_min_s: float = 5.0
    lag_max_s: float = 90.0
    # ComCat (TexNet in particular) adds and revises events for days, so the
    # newest part of the cache is re-fetched on every run.
    refresh_days: int = 7
    timeout_s: float = 30.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> RegionalConfig:
        c = dict(cfg or {})
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in c.items() if k in names})


@dataclass(frozen=True)
class RegionalMatch:
    """The regional event an origin was matched to, for the triage reason."""

    event_id: str
    magnitude: float
    distance_km: float
    place: str
    lag_s: float

    def describe(self) -> str:
        return (
            f"{self.lag_s:.0f} s after regional M{self.magnitude:.1f}, "
            f"{self.place} ({self.distance_km:.0f} km, {self.event_id})"
        )


def distance_km(lat0: float, lon0: float, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Great-circle distance from (lat0, lon0) to each point, in km."""
    p0, p = math.radians(lat0), np.radians(lat)
    dphi = p - p0
    dlmb = np.radians(lon) - math.radians(lon0)
    a = np.sin(dphi / 2) ** 2 + math.cos(p0) * np.cos(p) * np.sin(dlmb / 2) ** 2
    return np.asarray(2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a)))


def _utc(t) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


Span = tuple[pd.Timestamp, pd.Timestamp]


class RegionalCatalog:
    """Cached ComCat rows, the span they cover, and the subset that qualifies.

    `raw` is everything the query returned (all of the search radius), so
    changing `min_distance_km` or raising `min_magnitude` needs no re-fetch.
    `events` is the qualifying subset used for matching.
    """

    def __init__(self, raw: pd.DataFrame, cfg: RegionalConfig, covered: Span | None):
        self.cfg = cfg
        self.covered = covered
        self.raw = raw.reset_index(drop=True)
        ev = self.raw.copy()
        if not ev.empty:
            ev["time"] = pd.to_datetime(ev["time"], utc=True, format="ISO8601")
            keep = (
                (ev["mag"] >= cfg.min_magnitude)
                & (ev["distance_km"] > cfg.min_distance_km)
                & (ev["distance_km"] <= cfg.max_distance_km)
            )
            ev = ev[keep].sort_values("time").reset_index(drop=True)
        self.events = ev
        self._t = ev["time"].astype("int64").to_numpy() / 1e9 if not ev.empty else np.empty(0)

    def __len__(self) -> int:
        return len(self.events)

    def covers(self, t) -> bool:
        if self.covered is None:
            return False
        ts = _utc(t)
        start, end = self.covered
        return start + pd.Timedelta(seconds=self.cfg.lag_max_s) <= ts <= end

    def match(self, origin_time) -> RegionalMatch | None:
        """Most recent qualifying regional event 5-90 s before `origin_time`.

        Returns None when nothing matches or the time is outside coverage.
        """
        if not self.covers(origin_time) or self._t.size == 0:
            return None
        t = _utc(origin_time).value / 1e9
        lo = np.searchsorted(self._t, t - self.cfg.lag_max_s, side="left")
        hi = np.searchsorted(self._t, t - self.cfg.lag_min_s, side="right")
        if hi <= lo:
            return None
        row = self.events.iloc[hi - 1]
        return RegionalMatch(
            event_id=str(row["id"]),
            magnitude=float(row["mag"]),
            distance_km=float(row["distance_km"]),
            place=str(row["place"]),
            lag_s=t - self._t[hi - 1],
        )


# ── ComCat ────────────────────────────────────────────────────────────


def comcat_fetcher(cfg: RegionalConfig) -> Fetcher:
    """A Fetcher for the USGS event service with `cfg`'s region and magnitude."""

    def fetch(start: pd.Timestamp, end: pd.Timestamp) -> str:
        query = urllib.parse.urlencode(
            {
                "format": "csv",
                "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "latitude": cfg.reference_lat,
                "longitude": cfg.reference_lon,
                "maxradiuskm": cfg.max_distance_km,
                "minmagnitude": cfg.min_magnitude,
                "eventtype": "earthquake",
                "orderby": "time-asc",
                "limit": COMCAT_MAX_EVENTS,
            }
        )
        with urllib.request.urlopen(f"{COMCAT_URL}?{query}", timeout=cfg.timeout_s) as r:
            return str(r.read().decode("utf-8"))

    return fetch


def parse_comcat_csv(text: str, cfg: RegionalConfig) -> pd.DataFrame:
    """USGS CSV -> cache rows (CACHE_COLUMNS), with distance to the network."""
    if not text.strip():
        return pd.DataFrame(columns=CACHE_COLUMNS)
    df = pd.read_csv(io.StringIO(text))
    if len(df) >= COMCAT_MAX_EVENTS:
        raise ValueError(f"ComCat returned {len(df)} events, the per-query cap; split the span")
    df = df.dropna(subset=["time", "latitude", "longitude", "mag"])
    df["distance_km"] = distance_km(
        cfg.reference_lat, cfg.reference_lon, df["latitude"].to_numpy(), df["longitude"].to_numpy()
    )
    df["place"] = df.get("place", pd.Series("", index=df.index)).fillna("")
    return df[CACHE_COLUMNS].reset_index(drop=True)


# ── Cache ─────────────────────────────────────────────────────────────


def _coverage_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".json")


def load_cache(cache_path: Path, cfg: RegionalConfig) -> RegionalCatalog:
    """The cached catalog; empty with no coverage if the cache is missing."""
    meta_path = _coverage_path(cache_path)
    if not cache_path.exists() or not meta_path.exists():
        return RegionalCatalog(pd.DataFrame(columns=CACHE_COLUMNS), cfg, None)
    meta = json.loads(meta_path.read_text())
    covered = (_utc(meta["start"]), _utc(meta["end"]))
    return RegionalCatalog(pd.read_csv(cache_path), cfg, covered)


def _save_cache(cache_path: Path, raw: pd.DataFrame, covered: Span) -> None:
    out = raw[CACHE_COLUMNS].copy()
    out["time"] = pd.to_datetime(out["time"], utc=True, format="ISO8601").dt.strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    atomic_write_df(out.sort_values("time"), cache_path, index=False)
    meta = {"start": covered[0].isoformat(), "end": covered[1].isoformat()}
    atomic_write_text(json.dumps(meta, indent=2) + "\n", _coverage_path(cache_path))


def _chunks(span: Span, days: int = 365) -> list[Span]:
    """Split a span so no single query can approach COMCAT_MAX_EVENTS."""
    a, b = span
    out = []
    while a < b:
        nxt = min(a + pd.Timedelta(days=days), b)
        out.append((a, nxt))
        a = nxt
    return out


def _spans_to_fetch(covered: Span | None, want: Span, refresh_days: int) -> list[Span]:
    """Parts of `want` not cached yet, plus the last `refresh_days` before its end."""
    want_start, now = want
    if covered is None:
        return [want]
    start, end = covered
    spans = []
    if want_start < start:
        spans.append((want_start, start))
    spans.append((min(end, now - pd.Timedelta(days=refresh_days)), now))
    return spans


def refresh(
    cache_path: Path,
    cfg: RegionalConfig,
    need_from,
    now=None,
    fetch: Fetcher | None = None,
    logger: logging.Logger | None = None,
) -> RegionalCatalog:
    """Bring the cache up to date for origins from `need_from` on, and return it.

    Fetches whatever part of [need_from, now] is not cached yet, plus the
    last `refresh_days`. Cached rows inside a re-fetched span are replaced,
    because ComCat revises and deletes events. Never raises on a network or
    parse failure: logs a warning and returns the cache as it was.
    """
    log = logger or _default_log
    now_ts = _utc(now if now is not None else pd.Timestamp.now(tz="UTC"))
    want = (_utc(need_from) - pd.Timedelta(seconds=cfg.lag_max_s), now_ts)
    cached = load_cache(cache_path, cfg)
    spans = [c for s in _spans_to_fetch(cached.covered, want, cfg.refresh_days) for c in _chunks(s)]
    fetch = fetch or comcat_fetcher(cfg)
    try:
        fetched = [parse_comcat_csv(fetch(a, b), cfg) for a, b in spans]
    except Exception as exc:  # network, HTTP or parse: degrade to the cache
        log.warning("ComCat refresh failed (%s); using cached regional events", exc)
        return cached

    keep = cached.raw
    if not keep.empty:
        times = pd.to_datetime(keep["time"], utc=True, format="ISO8601")
        stale = np.zeros(len(keep), dtype=bool)
        for a, b in spans:
            stale |= ((times >= a) & (times <= b)).to_numpy()
        keep = keep[~stale]
    frames = [f for f in [keep, *fetched] if not f.empty]
    raw = (
        pd.concat(frames, ignore_index=True).drop_duplicates("id", keep="last")
        if frames
        else pd.DataFrame(columns=CACHE_COLUMNS)
    )
    covered = (
        (want[0], now_ts) if cached.covered is None else (min(cached.covered[0], want[0]), now_ts)
    )
    _save_cache(cache_path, raw, covered)
    log.info(
        "Regional events cached: %d rows, %s to %s",
        len(raw),
        covered[0].date(),
        covered[1].date(),
    )
    return RegionalCatalog(raw, cfg, covered)
