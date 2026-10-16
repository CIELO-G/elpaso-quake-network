"""lib/regional: ComCat parsing, the 5-90 s match, the cache, and its wiring
into triage and the catalog step. No network: every test injects a fetcher."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lib.regional import (
    COMCAT_MAX_EVENTS,
    RegionalCatalog,
    RegionalConfig,
    distance_km,
    load_cache,
    parse_comcat_csv,
    refresh,
)
from lib.triage import AUTO_REJECT, OK, TriageConfig, apply_triage, triage_event

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "5-catalog"))
from catalog import earliest_origin, load_regional  # noqa: E402

# Explicit thresholds, so the matching tests do not depend on the shipped defaults.
CFG = RegionalConfig(enabled=True, min_magnitude=2.0, max_distance_km=700.0)
# A Delaware Basin point ~232 km east of the network, and one ~30 km away.
FAR = (31.49, -103.986)
NEAR = (31.80, -106.10)
REF = (CFG.reference_lat, CFG.reference_lon)
HEADER = "time,latitude,longitude,depth,mag,magType,id,place,type\n"


def usgs_csv(rows: list[tuple]) -> str:
    """rows: (iso_time, (lat, lon), mag, id) -> USGS fdsnws CSV text."""
    lines = [f'{t},{ll[0]},{ll[1]},5.0,{m},ml,{i},"near {i}",earthquake' for t, ll, m, i in rows]
    return HEADER + "\n".join(lines) + ("\n" if lines else "")


def catalog_of(rows: list[tuple], covered=("2026-01-01", "2026-12-31")) -> RegionalCatalog:
    raw = parse_comcat_csv(usgs_csv(rows), CFG)
    return RegionalCatalog(
        raw, CFG, (pd.Timestamp(covered[0], tz="UTC"), pd.Timestamp(covered[1], tz="UTC"))
    )


class FakeComCat:
    """Fetcher stand-in: serves `rows` filtered by the requested span."""

    def __init__(self, rows: list[tuple]):
        self.rows = rows
        self.calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def __call__(self, start: pd.Timestamp, end: pd.Timestamp) -> str:
        self.calls.append((start, end))
        keep = [r for r in self.rows if start <= pd.Timestamp(r[0]) <= end]
        return usgs_csv(keep)


# ── geometry and parsing ─────────────────────────────────────────────


def test_distance_km_agrees_with_wgs84_geodesic():
    from pyproj import Geod

    lats = np.array([FAR[0], NEAR[0], 28.6, 34.4])
    lons = np.array([FAR[1], NEAR[1], -100.2, -107.9])
    d = distance_km(CFG.reference_lat, CFG.reference_lon, lats, lons)
    _, _, ref_m = Geod(ellps="WGS84").inv(
        np.full(4, CFG.reference_lon), np.full(4, CFG.reference_lat), lons, lats
    )
    np.testing.assert_allclose(d, ref_m / 1000, rtol=0.005)  # sphere vs ellipsoid
    assert distance_km(0.0, 0.0, np.array([0.0]), np.array([0.0]))[0] == 0.0


def test_parse_keeps_cache_columns_and_adds_distance():
    df = parse_comcat_csv(usgs_csv([("2026-03-07T10:00:00.000Z", FAR, 3.1, "tx1")]), CFG)
    assert list(df.columns) == [
        "id",
        "time",
        "latitude",
        "longitude",
        "mag",
        "place",
        "distance_km",
    ]
    assert df.loc[0, "id"] == "tx1" and df.loc[0, "place"] == "near tx1"
    assert 225 < df.loc[0, "distance_km"] < 240


def test_parse_empty_and_cap():
    assert parse_comcat_csv("", CFG).empty
    assert parse_comcat_csv(HEADER, CFG).empty
    rows = [
        (f"2026-01-01T00:00:{i % 60:02d}Z", FAR, 2.5, f"e{i}") for i in range(COMCAT_MAX_EVENTS)
    ]
    with pytest.raises(ValueError, match="per-query cap"):
        parse_comcat_csv(usgs_csv(rows), CFG)


# ── matching ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "lag_s, hit",
    [(4.9, False), (5.0, True), (45.0, True), (90.0, True), (90.1, False), (-10.0, False)],
)
def test_match_window_edges(lag_s, hit):
    cat = catalog_of([("2026-03-07T10:00:00Z", FAR, 3.1, "tx1")])
    origin = pd.Timestamp("2026-03-07T10:00:00Z") + pd.Timedelta(seconds=lag_s)
    m = cat.match(origin)
    assert (m is not None) == hit
    if hit:
        assert m.event_id == "tx1" and m.lag_s == pytest.approx(lag_s)


def test_match_ignores_small_near_and_out_of_range_events():
    far_away = (39.0, -100.0)  # > 700 km
    cat = catalog_of(
        [
            ("2026-03-07T10:00:00Z", FAR, 1.9, "small"),
            ("2026-03-07T11:00:00Z", NEAR, 3.0, "near"),
            ("2026-03-07T12:00:00Z", far_away, 4.0, "far"),
        ]
    )
    for hour in (10, 11, 12):
        assert cat.match(pd.Timestamp(f"2026-03-07T{hour}:00:30Z")) is None
    assert len(cat) == 0 and len(cat.raw) == 3


def test_match_picks_most_recent_and_accepts_naive_times():
    cat = catalog_of(
        [("2026-03-07T10:00:00Z", FAR, 2.8, "first"), ("2026-03-07T10:00:40Z", FAR, 3.3, "second")]
    )
    m = cat.match("2026-03-07T10:01:00")  # catalog times are naive UTC strings
    assert m is not None and m.event_id == "second" and m.lag_s == pytest.approx(20.0)
    z = cat.match("2026-03-07T10:01:00.000000Z")  # ...or, after a dashboard save, Z-suffixed
    assert z is not None and z.lag_s == pytest.approx(20.0)
    assert "20 s after regional M3.3" in m.describe() and "km, second)" in m.describe()


def test_no_match_outside_coverage():
    cat = catalog_of(
        [("2026-03-07T10:00:00Z", FAR, 3.1, "tx1")], covered=("2026-03-07T11:00:00", "2026-12-31")
    )
    assert cat.match("2026-03-07T10:00:30Z") is None
    assert RegionalCatalog(pd.DataFrame(), CFG, None).match("2026-03-07T10:00:30Z") is None


# ── cache refresh ────────────────────────────────────────────────────


def test_first_refresh_backfills_in_year_chunks(tmp_path):
    cache = tmp_path / "comcat_regional.csv"
    fake = FakeComCat(
        [("2024-05-01T00:00:00Z", FAR, 3.0, "old"), ("2026-03-07T10:00:00Z", FAR, 3.1, "new")]
    )
    cat = refresh(cache, CFG, "2024-01-01", now="2026-03-08", fetch=fake)
    assert len(fake.calls) == 3  # 2024-01 -> 2026-03 is just over two years
    assert fake.calls[0][0] == pd.Timestamp("2024-01-01", tz="UTC") - pd.Timedelta(seconds=90)
    assert fake.calls[-1][1] == pd.Timestamp("2026-03-08", tz="UTC")
    assert set(cat.events["id"]) == {"old", "new"}
    assert load_cache(cache, CFG).covered == cat.covered


def test_later_refresh_refetches_recent_days_and_new_gaps_only(tmp_path):
    cache = tmp_path / "comcat_regional.csv"
    fake = FakeComCat([("2026-03-01T00:00:00Z", FAR, 3.0, "a")])
    refresh(cache, CFG, "2026-02-01", now="2026-03-08", fetch=fake)
    fake.calls.clear()
    refresh(cache, CFG, "2026-02-01", now="2026-03-10", fetch=fake)
    assert fake.calls == [
        (pd.Timestamp("2026-03-03", tz="UTC"), pd.Timestamp("2026-03-10", tz="UTC"))
    ]
    fake.calls.clear()
    refresh(cache, CFG, "2026-01-15", now="2026-03-10", fetch=fake)  # catalog grew backwards
    assert fake.calls[0][0] == pd.Timestamp("2026-01-15", tz="UTC") - pd.Timedelta(seconds=90)


def test_refresh_replaces_rows_comcat_revised_or_deleted(tmp_path):
    cache = tmp_path / "comcat_regional.csv"
    fake = FakeComCat(
        [("2026-03-07T10:00:00Z", FAR, 3.1, "kept"), ("2026-03-07T11:00:00Z", FAR, 2.4, "deleted")]
    )
    refresh(cache, CFG, "2026-03-01", now="2026-03-08", fetch=fake)
    fake.rows = [("2026-03-07T10:00:00Z", FAR, 3.4, "kept")]  # revised magnitude, one deleted
    cat = refresh(cache, CFG, "2026-03-01", now="2026-03-09", fetch=fake)
    assert list(cat.events["id"]) == ["kept"]
    assert cat.events.loc[0, "mag"] == pytest.approx(3.4)


def test_refresh_failure_keeps_cache_and_logs(tmp_path, caplog):
    cache = tmp_path / "comcat_regional.csv"
    refresh(
        cache,
        CFG,
        "2026-03-01",
        now="2026-03-08",
        fetch=FakeComCat([("2026-03-07T10:00:00Z", FAR, 3.1, "tx1")]),
    )

    def down(start, end):
        raise OSError("network unreachable")

    with caplog.at_level(logging.WARNING):
        cat = refresh(cache, CFG, "2026-03-01", now="2026-03-09", fetch=down)
    assert "ComCat refresh failed" in caplog.text
    assert list(cat.events["id"]) == ["tx1"]
    assert cat.covered[1] == pd.Timestamp("2026-03-08", tz="UTC")  # coverage not extended


def test_refresh_failure_without_cache_tags_nothing(tmp_path):
    def down(start, end):
        raise OSError("network unreachable")

    cat = refresh(tmp_path / "c.csv", CFG, "2026-03-01", now="2026-03-08", fetch=down)
    assert cat.covered is None and cat.match("2026-03-07T10:00:30Z") is None


# ── triage wiring ────────────────────────────────────────────────────

CORE = ["KIDD", "R0F2D", "R4B41", "RE2E7", "R3B56"]


def test_triage_event_rejects_regional_match():
    m = catalog_of([("2026-03-07T10:00:00Z", FAR, 3.1, "tx1")]).match("2026-03-07T10:00:30Z")
    assert triage_event(1.0, CORE, TriageConfig(), None) == (OK, "")
    label, reason = triage_event(1.0, CORE, TriageConfig(), m)
    assert label == AUTO_REJECT and "tx1" in reason


def test_triage_config_reads_nested_regional_block():
    cfg = TriageConfig.from_config(
        {"regional": {"enabled": True, "min_magnitude": 2.5, "bogus": 1}}
    )
    assert cfg.regional.enabled and cfg.regional.min_magnitude == 2.5
    assert TriageConfig.from_config(None).regional.enabled is False


def test_shipped_config_tags_only_m3_within_350_km():
    import yaml

    cfg_path = Path(__file__).resolve().parent.parent / "5-catalog" / "config.yaml"
    shipped = TriageConfig.from_config(yaml.safe_load(cfg_path.read_text())["triage"]).regional
    assert shipped.enabled and shipped.min_magnitude == 3.0 and shipped.max_distance_km == 350.0
    # ~400 km NE: beyond the 350 km cap, however large.
    beyond = (34.5, -104.0)
    assert distance_km(*REF, np.array([beyond[0]]), np.array([beyond[1]]))[0] > 350
    rows = [
        ("2026-03-07T10:00:00Z", FAR, 2.9, "m29"),
        ("2026-03-07T11:00:00Z", FAR, 3.0, "m30"),
        ("2026-03-07T12:00:00Z", beyond, 4.5, "beyond"),
    ]
    raw = parse_comcat_csv(usgs_csv(rows), shipped)
    cat = RegionalCatalog(
        raw, shipped, (pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-12-31", tz="UTC"))
    )
    hits = {h: cat.match(pd.Timestamp(f"2026-03-07T{h}:00:30Z")) for h in (10, 11, 12)}
    assert hits[10] is None and hits[12] is None
    assert hits[11] is not None and hits[11].event_id == "m30"


def _catalog_and_picks():
    cat = pd.DataFrame(
        [
            {
                "event_id": "ep1",
                "time": "2026-03-07T10:00:30.000",
                "magnitude": 1.0,
                "review_status": "confirmed",
            },
            {
                "event_id": "ep2",
                "time": "2026-03-07T15:00:00.000",
                "magnitude": 1.0,
                "review_status": "",
            },
        ]
    )
    asg = pd.DataFrame([{"event_id": e, "station": s} for e in ("ep1", "ep2") for s in CORE])
    return cat, asg


def test_apply_triage_uses_regional_only_when_enabled():
    cat, asg = _catalog_and_picks()
    regional = catalog_of([("2026-03-07T10:00:00Z", FAR, 3.1, "tx1")])
    on = apply_triage(cat, asg, TriageConfig(regional=CFG), regional).set_index("event_id")
    assert on.loc["ep1", "triage"] == AUTO_REJECT and on.loc["ep2", "triage"] == OK
    assert on.loc["ep1", "review_status"] == "confirmed"  # labels only
    off = apply_triage(cat, asg, TriageConfig(), regional)
    assert set(off["triage"]) == {OK}
    assert set(apply_triage(cat, asg, TriageConfig(regional=CFG), None)["triage"]) == {OK}


# ── catalog step helpers ─────────────────────────────────────────────


def test_earliest_origin_covers_catalog_and_new_days(tmp_path):
    path = tmp_path / "catalog.csv"
    assert earliest_origin(path, []) is None
    # Both formats occur in the live catalog: pipeline rows are naive, rows
    # saved by the dashboard carry microseconds and a Z.
    times = ["2026-03-07T10:00:30.000", "2025-11-01T01:00:00.000", "2025-12-01T00:00:00.123456Z"]
    pd.DataFrame({"time": times}).to_csv(path)
    assert earliest_origin(path, []) == pd.Timestamp("2025-11-01T01:00:00", tz="UTC")
    assert earliest_origin(path, [{"day_key": "20240101"}]) == pd.Timestamp("2024-01-01", tz="UTC")


def test_load_regional_is_off_unless_enabled(tmp_path):
    log = logging.getLogger("test")
    t0 = pd.Timestamp("2026-03-01", tz="UTC")
    assert load_regional(TriageConfig(), tmp_path / "c.csv", t0, log) is None
    assert load_regional(TriageConfig(regional=CFG), tmp_path / "c.csv", None, log) is None
    assert (
        load_regional(TriageConfig(enabled=False, regional=CFG), tmp_path / "c.csv", t0, log)
        is None
    )
