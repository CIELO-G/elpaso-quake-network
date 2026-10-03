"""Export the public read-only website data bundle (pipeline step 6).

Writes static JSON consumed by the public site (../elpaso-seismic —
a separate repo deployed on GitHub Pages at
https://cielo-g.github.io/elpaso-seismic). ONLY confirmed events are
exported: rejected/unreviewed detections, review bookkeeping, logs and
credentials never leave this machine.

    python scripts/export_public_site.py           # export only
    python scripts/export_public_site.py --push    # export + commit + push

Run with the elpaso-quake env (needs obspy for waveform decimation).
Called best-effort by run_pipeline.py after each completed day.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
SITE = REPO.parent / "elpaso-seismic"
PROC = REPO / "output" / "2-processed"

sys.path.insert(0, str(REPO))

WAVE_SECONDS_MAX = 45.0
WAVE_POINTS = 700  # min/max pairs per trace — a few KB per station


def load_z(net: str, sta: str, t0, t1):
    from obspy import Stream, UTCDateTime, read

    day = UTCDateTime(t0.year, t0.month, t0.day)
    end = UTCDateTime(t1.year, t1.month, t1.day)
    chan = "HHZ" if net == "EP" else "EHZ"
    st = Stream()
    while day <= end:
        d = PROC / str(day.year) / f"{day.julday:03d}"
        for fp in d.glob(f"{net}.{sta}.*.{chan}.{day.year}.{day.julday:03d}.mseed"):
            try:
                st += read(str(fp))
            except Exception:
                pass
        day += 86400
    if not st:
        return None
    st.merge(fill_value=0)
    st.trim(t0, t1)
    if not st or len(st[0].data) < 200:
        return None
    tr = st[0]
    tr.detrend("demean")
    tr.filter("bandpass", freqmin=1.0, freqmax=12.0, corners=4, zerophase=True)
    return tr


def minmax_decimate(data: np.ndarray, n_bins: int):
    """[min, max] per bin — preserves waveform envelope at tiny size."""
    n = len(data)
    edges = np.linspace(0, n, n_bins + 1).astype(int)
    out = []
    peak = float(np.abs(data).max()) or 1.0
    for i in range(n_bins):
        seg = data[edges[i]:edges[i + 1]]
        if len(seg) == 0:
            out.append([0.0, 0.0])
        else:
            out.append([round(float(seg.min()) / peak, 4),
                       round(float(seg.max()) / peak, 4)])
    return out


def export() -> dict:
    from obspy import UTCDateTime
    from obspy.geodetics import gps2dist_azimuth

    cat = pd.read_csv(REPO / "output/5-catalog/catalog.csv")
    asg = pd.read_csv(REPO / "output/5-catalog/assignments.csv")
    stations = json.load(open(REPO / "stations.json"))
    sta_by_code = {s["station"]: s for s in stations}

    conf = cat[cat.review_status == "confirmed"].copy()
    conf = conf.sort_values("time", ascending=False)

    (SITE / "data" / "events").mkdir(parents=True, exist_ok=True)

    catalog_rows = []
    for _, ev in conf.iterrows():
        origin = UTCDateTime(ev.time)
        ev_asg = asg[asg.event_id == ev.event_id]
        picks = []
        traces = []
        for sta in ev_asg.station.unique():
            meta = sta_by_code.get(sta)
            if meta is None:
                continue
            dist_km = gps2dist_azimuth(
                ev.latitude, ev.longitude,
                meta["latitude"], meta["longitude"])[0] / 1000.0
            for p in ev_asg[ev_asg.station == sta].itertuples():
                picks.append({
                    "station": sta, "phase": p.phase,
                    "t": round(UTCDateTime(p.time) - origin, 3),
                })
            post = min(WAVE_SECONDS_MAX, dist_km / 2.0 + 15.0)
            tr = load_z(meta["network"], sta, origin - 2.0, origin + post)
            if tr is None:
                continue
            traces.append({
                "station": sta, "network": meta["network"],
                "distance_km": round(dist_km, 1),
                "t0": -2.0,
                "dt": round((post + 2.0) / WAVE_POINTS, 5),
                "minmax": minmax_decimate(tr.data.astype(float), WAVE_POINTS),
            })
        traces.sort(key=lambda t: t["distance_km"])

        detail = {
            "event_id": ev.event_id,
            "time": ev.time,
            "latitude": round(float(ev.latitude), 5),
            "longitude": round(float(ev.longitude), 5),
            "depth_km": round(float(ev.depth_km), 2) if pd.notna(ev.depth_km) else None,
            "magnitude": round(float(ev.magnitude), 2) if pd.notna(ev.magnitude) else None,
            "event_type": ev.event_type,
            "num_picks": int(ev.num_picks) if pd.notna(ev.num_picks) else len(picks),
            "picks": picks,
            "traces": traces,
        }
        (SITE / "data" / "events" / f"{ev.event_id}.json").write_text(
            json.dumps(detail, separators=(",", ":")))

        catalog_rows.append({k: detail[k] for k in (
            "event_id", "time", "latitude", "longitude", "depth_km",
            "magnitude", "event_type", "num_picks")})

    (SITE / "data" / "catalog.json").write_text(
        json.dumps(catalog_rows, separators=(",", ":")))

    pub_stations = [{
        "station": s["station"], "network": s["network"],
        "latitude": s["latitude"], "longitude": s["longitude"],
        "model": s.get("model", ""),
    } for s in stations]
    (SITE / "data" / "stations.json").write_text(
        json.dumps(pub_stations, separators=(",", ":")))

    n_blast = sum(1 for r in catalog_rows if r["event_type"] == "quarry_blast")
    n_eq = sum(1 for r in catalog_rows if r["event_type"] == "earthquake")
    meta_out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_events": len(catalog_rows),
        "n_quarry_blasts": n_blast,
        "n_earthquakes": n_eq,
        "n_stations": len(pub_stations),
    }
    (SITE / "data" / "meta.json").write_text(json.dumps(meta_out, indent=1))
    return meta_out


def push() -> None:
    if not (SITE / ".git").exists():
        print("site repo has no .git — skipping push")
        return
    subprocess.run(["git", "-C", str(SITE), "add", "-A"], check=True)
    diff = subprocess.run(["git", "-C", str(SITE), "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("no site changes to publish")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subprocess.run(["git", "-C", str(SITE), "commit", "-q", "-m",
                    f"Data update {stamp}"], check=True)
    subprocess.run(["git", "-C", str(SITE), "push", "-q"], check=True)
    print("site pushed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--push", action="store_true", help="commit+push the site repo")
    args = ap.parse_args()
    meta = export()
    print(f"exported {meta['n_events']} events "
          f"({meta['n_quarry_blasts']} blasts, {meta['n_earthquakes']} eq) "
          f"-> {SITE}")
    if args.push:
        push()
