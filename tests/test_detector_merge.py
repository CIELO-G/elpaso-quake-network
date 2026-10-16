"""lib/detector_merge: which PhaseNO events the merge adds to PhaseNet's."""

from __future__ import annotations

import pandas as pd
import pytest

from lib.detector_merge import (
    CORROBORATION_COLUMN,
    MergeConfig,
    corroboration_counts,
    select_additions,
)

CFG = MergeConfig()
T0 = pd.Timestamp("2026-06-08T17:00:00")
STATIONS = ["KIDD", "R0F2D", "R4B41", "RE2E7"]


def iso(offset_s: float) -> str:
    return (T0 + pd.Timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def events(*rows: tuple[int, float]) -> pd.DataFrame:
    """(event_index, origin offset s) -> daily events CSV rows."""
    return pd.DataFrame(
        [{"event_index": i, "time": iso(t), "magnitude": 1.0, "num_picks": 4} for i, t in rows]
    )


def picks(*rows: tuple, network: str = "AM") -> pd.DataFrame:
    """(station, phase, offset s[, event_index]) -> picks / assignments rows."""
    out = []
    for r in rows:
        row = {
            "network": network,
            "station": r[0],
            "location": "00",
            "channel": "EHZ",
            "phase": r[1],
            "time": iso(r[2]),
            "probability": 0.6,
        }
        if len(r) > 3:
            row["event_index"] = r[3]
        out.append(row)
    return pd.DataFrame(out)


def secondary_event(index: int, origin_s: float, n_picks: int = 4):
    """One PhaseNO event with P picks on the first n stations, 5 s after origin."""
    ev = events((index, origin_s))
    asg = picks(*[(s, "P", origin_s + 5 + k, index) for k, s in enumerate(STATIONS[:n_picks])])
    return ev, asg


def primary_picks_for(asg: pd.DataFrame, n: int, shift_s: float = 0.0) -> pd.DataFrame:
    """PhaseNet picks matching the first n PhaseNO picks, shifted by shift_s."""
    rows = asg.iloc[:n]
    t = (pd.to_datetime(rows["time"]) - T0).dt.total_seconds() + shift_s
    return picks(*zip(rows["station"], rows["phase"], t))


def test_adds_missed_event_corroborated_by_three_picks():
    ev, asg = secondary_event(7, 100.0)
    added, added_picks = select_additions(
        events((0, 500.0)), primary_picks_for(asg, 3), ev, asg, CFG
    )
    assert added["event_index"].tolist() == [7]
    assert added[CORROBORATION_COLUMN].tolist() == [3]
    assert len(added_picks) == 4 and set(added_picks["event_index"]) == {7}


def test_two_corroborating_picks_are_not_enough():
    ev, asg = secondary_event(7, 100.0)
    added, _ = select_additions(events(), primary_picks_for(asg, 2), ev, asg, CFG)
    assert added.empty


@pytest.mark.parametrize(
    "gap_s, kept", [(10.0, False), (-10.0, False), (10.1, True), (-10.1, True)]
)
def test_primary_event_within_separation_blocks_the_addition(gap_s, kept):
    ev, asg = secondary_event(7, 100.0)
    added, _ = select_additions(events((0, 100.0 + gap_s)), primary_picks_for(asg, 4), ev, asg, CFG)
    assert (not added.empty) == kept


@pytest.mark.parametrize("shift_s, hit", [(0.0, True), (1.0, True), (-1.0, True), (1.01, False)])
def test_corroboration_tolerance(shift_s, hit):
    _, asg = secondary_event(7, 100.0, n_picks=1)
    counts = corroboration_counts(asg, primary_picks_for(asg, 1, shift_s), 1.0)
    assert counts.to_dict() == {7: int(hit)}


def test_corroboration_needs_same_network_station_and_phase():
    _, asg = secondary_event(7, 100.0, n_picks=1)  # AM.KIDD P at +105 s
    others = pd.concat(
        [
            picks(("KIDD", "S", 105.0)),
            picks(("R0F2D", "P", 105.0)),
            picks(("KIDD", "P", 105.0), network="TX"),
        ]
    )
    assert corroboration_counts(asg, others, 1.0).to_dict() == {7: 0}


def test_each_event_counts_only_its_own_picks():
    ev_a, asg_a = secondary_event(1, 100.0)
    ev_b, asg_b = secondary_event(2, 1000.0)
    ev, asg = pd.concat([ev_a, ev_b]), pd.concat([asg_a, asg_b])
    primary = pd.concat([primary_picks_for(asg_a, 4), primary_picks_for(asg_b, 1)])
    added, added_picks = select_additions(events(), primary, ev, asg, CFG)
    assert added[["event_index", CORROBORATION_COLUMN]].values.tolist() == [[1, 4]]
    assert set(added_picks["event_index"]) == {1}


def test_no_primary_picks_means_no_additions():
    ev, asg = secondary_event(7, 100.0)
    added, added_picks = select_additions(events(), picks(), ev, asg, CFG)
    assert added.empty and added_picks.empty


def test_empty_secondary_day_keeps_the_schema():
    added, added_picks = select_additions(events((0, 1.0)), picks(), events(), picks(), CFG)
    assert added.empty and CORROBORATION_COLUMN in added.columns and added_picks.empty


def test_mixed_time_formats_compare_as_utc():
    ev, asg = secondary_event(7, 100.0)
    primary = primary_picks_for(asg, 3)
    primary["time"] = primary["time"] + "Z"
    blocking = events((0, 105.0))
    blocking["time"] = (T0 + pd.Timedelta(seconds=105)).isoformat() + ".000000Z"
    assert select_additions(events(), primary, ev, asg, CFG)[0]["event_index"].tolist() == [7]
    assert select_additions(blocking, primary, ev, asg, CFG)[0].empty


def test_config_from_yaml_block_ignores_unknown_keys():
    cfg = MergeConfig.from_config({"min_corroborating_picks": 2, "comment": "x"})
    assert cfg == MergeConfig(min_corroborating_picks=2)
    assert MergeConfig.from_config(None) == MergeConfig()
