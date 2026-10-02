"""Post-association triage: label likely-false events so the review
queue stops showing them first. NEVER changes review_status — a human
still decides; triage only orders the work.

Rules were measured against the reviewed catalog on 2026-09-30
(42 confirmed vs 47 rejected):

  auto_reject  fewer than `min_core_stations` picked core stations
               (kept 100 % of confirmed, removed 67 % of rejected), or
               magnitude >= `inflated_mag_min` from <= `inflated_mag_max_picks`
               picks (kept 98 %, removed 54 %). Together: 98 % / 80 %.
  flag         <= `unconstrained_max_picks` picks: real events exist here
               (9 of 42 confirmed) but their locations are unconstrained.
  ok           everything else.

Thresholds and the core-station list are config (5-catalog/config.yaml
`triage:`), because they encode THIS network's geometry, not physics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

AUTO_REJECT = "auto_reject"
FLAG = "flag"
OK = "ok"
LABELS = (OK, FLAG, AUTO_REJECT)


@dataclass
class TriageConfig:
    enabled: bool = True
    core_stations: list[str] = field(
        default_factory=lambda: ["KIDD", "R0F2D", "R4B41", "RE2E7"])
    min_core_stations: int = 2
    inflated_mag_min: float = 2.2
    inflated_mag_max_picks: int = 4
    unconstrained_max_picks: int = 4

    @classmethod
    def from_config(cls, cfg: dict | None) -> "TriageConfig":
        c = dict(cfg or {})
        kw = {k: c[k] for k in cls.__dataclass_fields__ if k in c}
        return cls(**kw)


def triage_event(magnitude, pick_stations, cfg: TriageConfig) -> tuple[str, str]:
    """Label one event from its magnitude and the stations that picked it.

    Returns (label, reason). Multiple reasons are joined with '; '.
    """
    stations = {str(s) for s in pick_stations if isinstance(s, str) or not pd.isna(s)}
    n_picks = len(list(pick_stations))
    n_core = len(stations & set(cfg.core_stations))
    reasons: list[str] = []
    label = OK

    if n_core < cfg.min_core_stations:
        reasons.append(f"{n_core} core station(s) < {cfg.min_core_stations}")
        label = AUTO_REJECT
    try:
        mag = float(magnitude)
    except (TypeError, ValueError):
        mag = None
    if mag is not None and mag >= cfg.inflated_mag_min and n_picks <= cfg.inflated_mag_max_picks:
        reasons.append(f"M{mag:.1f} from {n_picks} picks")
        label = AUTO_REJECT
    if label != AUTO_REJECT and n_picks <= cfg.unconstrained_max_picks:
        reasons.append(f"{n_picks} picks (unconstrained)")
        label = FLAG
    return label, "; ".join(reasons)


def apply_triage(catalog: pd.DataFrame, assignments: pd.DataFrame,
                 cfg: TriageConfig) -> pd.DataFrame:
    """Add/refresh `triage` and `triage_reason` columns for EVERY row.

    Reviewed rows are labelled too (informational — lets you audit the
    rules against your own decisions); review_status is never touched.
    """
    out = catalog.copy()
    if not cfg.enabled or out.empty:
        out["triage"] = out.get("triage", "")
        out["triage_reason"] = out.get("triage_reason", "")
        return out
    picks_by_event: dict[str, list] = {}
    if assignments is not None and not assignments.empty and "event_id" in assignments:
        for eid, grp in assignments.groupby("event_id"):
            picks_by_event[str(eid)] = grp["station"].tolist()
    labels, reasons = [], []
    for row in out.to_dict("records"):
        lab, why = triage_event(row.get("magnitude"),
                                picks_by_event.get(str(row.get("event_id")), []), cfg)
        labels.append(lab); reasons.append(why)
    out["triage"] = labels
    out["triage_reason"] = reasons
    return out
