"""Event-level merge of a second detector (PhaseNO) into the primary one (PhaseNet).

The two pick sets are associated separately. Every primary event is kept as
it is; a secondary event is added only when

  * no primary event has an origin within `min_separation_s` of it (it is an
    event the primary pipeline missed, not a second solution of one it has),
    and
  * at least `min_corroborating_picks` of its picks have a primary pick of
    the same station and phase within `corroboration_tolerance_s` (PhaseNet
    saw the arrivals but did not associate them).

Measured in research/phaseno-eval (Round 2, FINDINGS.md): with 3 corroborating
picks every PhaseNO addition in the 80 reviewed windows was a confirmed event,
and on 20 full days the merge found 15/17 confirmed events against 12/17 for
PhaseNet alone. Mixing both pick sets into one association run was worse:
near-duplicate picks shift or split GaMMA's clusters.

Pure functions over DataFrames in the pipeline's daily CSV schemas; the
PhaseNO step (3-detection/phaseno/step.py) does the I/O.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

CORROBORATION_COLUMN = "corroborating_picks"
_PICK_KEY = ["network", "station", "phase"]


@dataclass(frozen=True)
class MergeConfig:
    min_corroborating_picks: int = 3
    corroboration_tolerance_s: float = 1.0
    min_separation_s: float = 10.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> MergeConfig:
        c = dict(cfg or {})
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in c.items() if k in names})


def _epoch_s(times: pd.Series) -> np.ndarray:
    """ISO times (naive = UTC, or 'Z'-suffixed) -> float seconds since 1970."""
    t = pd.to_datetime(times, utc=True, format="ISO8601")
    seconds: np.ndarray = (t - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().to_numpy()
    return seconds


def _nearest_gap(sorted_ref: np.ndarray, t: np.ndarray) -> np.ndarray:
    """|t - nearest element of sorted_ref| for each t (inf when ref is empty)."""
    if sorted_ref.size == 0:
        return np.full(t.shape, np.inf)
    i = np.searchsorted(sorted_ref, t)
    left = sorted_ref[np.clip(i - 1, 0, sorted_ref.size - 1)]
    right = sorted_ref[np.clip(i, 0, sorted_ref.size - 1)]
    gap: np.ndarray = np.minimum(np.abs(t - left), np.abs(t - right))
    return gap


def corroboration_counts(
    assignments: pd.DataFrame, primary_picks: pd.DataFrame, tolerance_s: float
) -> pd.Series:
    """Per secondary event_index: how many of its picks the primary picker also made.

    A pick is corroborated by a primary pick of the same network, station and
    phase within `tolerance_s`. Events with no corroborated pick count 0.
    """
    if assignments.empty:
        return pd.Series(dtype=int, name=CORROBORATION_COLUMN)
    hit = np.zeros(len(assignments), dtype=bool)
    if not primary_picks.empty:
        ref: dict[Hashable, np.ndarray] = {
            key: np.sort(_epoch_s(grp["time"]))
            for key, grp in primary_picks.groupby(_PICK_KEY, sort=False)
        }
        sec = assignments.reset_index(drop=True)
        t = _epoch_s(sec["time"])
        for key, rows in sec.groupby(_PICK_KEY, sort=False).indices.items():
            if key in ref:
                hit[rows] = _nearest_gap(ref[key], t[rows]) <= tolerance_s
    counts = pd.Series(hit.astype(int), index=assignments["event_index"].to_numpy())
    return counts.groupby(level=0).sum().rename(CORROBORATION_COLUMN)


def select_additions(
    primary_events: pd.DataFrame,
    primary_picks: pd.DataFrame,
    secondary_events: pd.DataFrame,
    secondary_assignments: pd.DataFrame,
    cfg: MergeConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Secondary events (and their picks) that the merge adds to the primary set.

    Returns the selected rows of `secondary_events`, with a
    `corroborating_picks` column, and the matching rows of
    `secondary_assignments`. Inputs are one day's daily CSVs.
    """
    if secondary_events.empty:
        return secondary_events.assign(**{CORROBORATION_COLUMN: []}), secondary_assignments.iloc[:0]
    events = secondary_events.copy()
    counts = corroboration_counts(
        secondary_assignments, primary_picks, cfg.corroboration_tolerance_s
    )
    events[CORROBORATION_COLUMN] = (
        events["event_index"].map(counts).fillna(0).astype(int).to_numpy()
    )
    primary_t = (
        np.sort(_epoch_s(primary_events["time"])) if not primary_events.empty else np.array([])
    )
    separated = _nearest_gap(primary_t, _epoch_s(events["time"])) > cfg.min_separation_s
    corroborated = events[CORROBORATION_COLUMN].to_numpy() >= cfg.min_corroborating_picks
    added = events[separated & corroborated].reset_index(drop=True)
    picks = secondary_assignments[
        secondary_assignments["event_index"].isin(added["event_index"])
    ].reset_index(drop=True)
    return added, picks
