"""lib/triage: rule semantics, config plumbing, review_status untouched."""
import pandas as pd
from lib.triage import AUTO_REJECT, FLAG, OK, TriageConfig, apply_triage, triage_event

CFG = TriageConfig()


def test_core_station_rule():
    lab, why = triage_event(1.0, ["S50BE", "RDB14", "RC9B8", "R8DE2", "R9070"], CFG)
    assert lab == AUTO_REJECT and "0 core" in why
    lab, _ = triage_event(1.0, ["KIDD", "S50BE", "RDB14", "RC9B8", "R9070"], CFG)
    assert lab == AUTO_REJECT                       # 1 core < 2
    lab, why = triage_event(1.0, ["KIDD", "R0F2D", "S50BE", "RDB14", "RC9B8"], CFG)
    assert lab == OK and why == ""


def test_inflated_magnitude_rule():
    lab, why = triage_event(2.5, ["KIDD", "R0F2D", "R4B41", "RE2E7"], CFG)
    assert lab == AUTO_REJECT and "M2.5 from 4 picks" in why
    lab, _ = triage_event(2.5, ["KIDD", "R0F2D", "R4B41", "RE2E7", "R3B56"], CFG)
    assert lab == FLAG or lab == OK                 # 5 picks: rule does not fire
    lab, _ = triage_event(2.1, ["KIDD", "R0F2D", "R4B41", "RE2E7"], CFG)
    assert lab == FLAG                              # below threshold -> just unconstrained


def test_unconstrained_flag_and_ok():
    assert triage_event(1.3, ["KIDD", "R0F2D", "R4B41", "RE2E7"], CFG)[0] == FLAG
    assert triage_event(1.3, ["KIDD", "R0F2D", "R4B41", "RE2E7", "R3B56"], CFG)[0] == OK
    assert triage_event(None, ["KIDD", "R0F2D", "X", "Y", "Z"], CFG)[0] == OK   # no magnitude: mag rule skipped
    assert triage_event("", ["KIDD", "R0F2D"], CFG)[0] == FLAG


def test_config_from_yaml_dict_and_disable():
    cfg = TriageConfig.from_config({"min_core_stations": 1, "core_stations": ["KIDD"], "bogus": 1})
    assert cfg.min_core_stations == 1 and cfg.core_stations == ["KIDD"]
    assert triage_event(1.0, ["KIDD", "A", "B", "C", "D"], cfg)[0] == OK
    off = TriageConfig(enabled=False)
    cat = pd.DataFrame([{"event_id": "e1", "magnitude": 3.0, "review_status": "confirmed"}])
    out = apply_triage(cat, pd.DataFrame(), off)
    assert list(out.triage) == [""] and out.review_status.tolist() == ["confirmed"]


def test_apply_triage_labels_all_rows_and_preserves_review():
    cat = pd.DataFrame([
        {"event_id": "good", "magnitude": 1.2, "review_status": "confirmed", "event_type": "quarry_blast"},
        {"event_id": "junk", "magnitude": 2.6, "review_status": "", "event_type": ""},
        {"event_id": "thin", "magnitude": 1.0, "review_status": "", "event_type": ""},
        {"event_id": "nopicks", "magnitude": 1.0, "review_status": "", "event_type": ""},
    ])
    asg = pd.DataFrame(
        [{"event_id": "good", "station": s} for s in ["KIDD", "R0F2D", "R4B41", "RE2E7", "R3B56"]] +
        [{"event_id": "junk", "station": s} for s in ["S50BE", "RDB14", "RC9B8", "R9070"]] +
        [{"event_id": "thin", "station": s} for s in ["KIDD", "R0F2D", "R4B41", "RE2E7"]])
    out = apply_triage(cat, asg, CFG).set_index("event_id")
    assert out.loc["good", "triage"] == OK
    assert out.loc["junk", "triage"] == AUTO_REJECT and ";" in out.loc["junk", "triage_reason"]
    assert out.loc["thin", "triage"] == FLAG
    assert out.loc["nopicks", "triage"] == AUTO_REJECT          # 0 core stations
    assert out.review_status.tolist() == ["confirmed", "", "", ""]
    assert out.event_type.tolist() == ["quarry_blast", "", "", ""]
