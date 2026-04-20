#!/usr/bin/env python3
"""
compute_batch.py — Batch SNR computation for 100+ regional earthquakes.

Processes all events from batch_events.json across all stations.
Features:
  - Waveform cache: downloaded waveforms saved to output/cache/ for reuse
  - Rate limiting: paced FDSN requests to avoid 429s
  - Resume: skips events already in the output CSV
  - Progress tracking: prints running totals

Usage:
    python compute_batch.py
    python compute_batch.py --resume                # skip already-computed events
    python compute_batch.py --delay 2.0             # seconds between FDSN requests
    python compute_batch.py --skip-fdsn             # only use processed data, no downloads
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from obspy import UTCDateTime, Stream, read, read_inventory
from obspy.clients.fdsn import Client
from obspy.taup import TauPyModel

from utils import (
    load_config, load_stations, station_distance_azimuth,
    get_site_type, compute_rms, snr_ratio, snr_db,
)

warnings.filterwarnings("ignore", message=".*No matching response.*")
warnings.filterwarnings("ignore", message=".*sac.*")


# --- FDSN client pool (reuse connections) ---
_clients: dict[str, Client] = {}


def get_client(source: str) -> Client:
    if source not in _clients:
        _clients[source] = Client(source)
    return _clients[source]


# --- Waveform cache ---

def cache_path(cache_dir: Path, net: str, sta: str, event_id: str) -> Path:
    return cache_dir / f"{event_id}_{net}.{sta}.mseed"


def load_cached(cache_dir: Path, net: str, sta: str, event_id: str) -> Stream | None:
    fp = cache_path(cache_dir, net, sta, event_id)
    if fp.exists():
        try:
            return read(str(fp))
        except Exception:
            fp.unlink(missing_ok=True)
    return None


def save_to_cache(st: Stream, cache_dir: Path, net: str, sta: str, event_id: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = cache_path(cache_dir, net, sta, event_id)
    try:
        st.write(str(fp), format="MSEED")
    except Exception:
        pass


# --- Theoretical arrivals ---

def get_arrivals(model: TauPyModel, depth_km: float, dist_km: float):
    dist_deg = dist_km / 111.19
    p, s = None, None
    try:
        arr = model.get_travel_times(
            source_depth_in_km=max(depth_km, 0.01),
            distance_in_degree=dist_deg,
            phase_list=["P", "p", "Pn", "Pg"],
        )
        if arr:
            p = arr[0].time
    except Exception:
        pass
    try:
        arr = model.get_travel_times(
            source_depth_in_km=max(depth_km, 0.01),
            distance_in_degree=dist_deg,
            phase_list=["S", "s", "Sn", "Sg"],
        )
        if arr:
            s = arr[0].time
    except Exception:
        pass
    return p, s


# --- Waveform loading ---

def load_processed(processed_dir: str, net: str, sta: str,
                   origin: UTCDateTime, t_start: UTCDateTime, t_end: UTCDateTime) -> Stream | None:
    st = Stream()
    for offset in range(-1, 2):
        t = origin + offset * 86400
        day_dir = Path(processed_dir) / str(t.year) / f"{t.julday:03d}"
        if not day_dir.is_dir():
            continue
        for fp in day_dir.glob(f"{net}.{sta}.*.mseed"):
            try:
                st += read(str(fp))
            except Exception:
                continue
    if len(st) == 0:
        return None
    try:
        st.merge(method=1, fill_value="interpolate")
        st.trim(starttime=t_start - 5, endtime=t_end + 5)
    except Exception:
        pass
    return st if len(st) > 0 else None


def fetch_fdsn(station: dict, t_start: UTCDateTime, t_end: UTCDateTime,
               fdsn_sources: dict, proc_cfg: dict, delay: float) -> Stream | None:
    net = station["network"]
    sta = station["station"]
    source = fdsn_sources.get(net)
    if not source:
        return None

    model = station.get("model", "")
    if model == "RS4D":
        chan = "EHZ"
    elif station.get("channels") == "HH?":
        chan = "HH*"
    else:
        chan = "EH*"

    loc = station.get("location", "00")

    time.sleep(delay)  # rate limit

    client = get_client(source)
    try:
        st = client.get_waveforms(
            network=net, station=sta, location=loc, channel=chan,
            starttime=t_start - 30, endtime=t_end + 30,
        )
    except Exception:
        return None

    if len(st) == 0:
        return None

    try:
        st.merge(method=1, fill_value="interpolate")
        st.detrend("demean")
        st.detrend("linear")
        st.taper(max_percentage=0.05, type="hann")

        inv = client.get_stations(
            network=net, station=sta, location=loc, channel=chan,
            starttime=t_start, endtime=t_end,
            level="response",
        )
        st.remove_response(
            inventory=inv,
            output=proc_cfg["output"],
            pre_filt=tuple(proc_cfg["pre_filt"]),
            water_level=proc_cfg["water_level"],
        )
    except Exception:
        try:
            st.detrend("demean")
            st.filter("bandpass",
                       freqmin=proc_cfg["filter_freqmin"],
                       freqmax=proc_cfg["filter_freqmax"],
                       zerophase=True)
        except Exception:
            pass

    return st if len(st) > 0 else None


# --- SNR computation ---

def measure_snr(tr, origin: UTCDateTime, p_travel: float, s_travel: float,
                snr_cfg: dict, freq_bands: dict) -> dict:
    """Compute separate P-phase, S-phase, and combined SNR in each frequency band."""
    results = {}
    p_abs = origin + p_travel
    s_abs = origin + s_travel
    noise_end = p_abs - snr_cfg["noise_gap_sec"]
    noise_start = noise_end - snr_cfg["noise_window_sec"]

    # Three signal windows
    windows = {
        "p": (p_abs, s_abs),                                          # P arrival → S arrival
        "s": (s_abs, s_abs + snr_cfg["signal_after_s_sec"]),          # S arrival → S + 10s
        "combined": (p_abs, s_abs + snr_cfg["signal_after_s_sec"]),   # P arrival → S + 10s
    }

    for band, (fmin, fmax) in freq_bands.items():
        try:
            filt = tr.copy()
            filt.filter("bandpass", freqmin=fmin, freqmax=fmax, zerophase=True)
            noise = filt.slice(starttime=noise_start, endtime=noise_end)
        except Exception:
            for phase in ("p", "s", "combined"):
                results[f"snr_{phase}_{band}"] = np.nan
                results[f"snr_db_{phase}_{band}"] = np.nan
            continue

        if noise is None or len(noise.data) < 10:
            for phase in ("p", "s", "combined"):
                results[f"snr_{phase}_{band}"] = np.nan
                results[f"snr_db_{phase}_{band}"] = np.nan
            continue

        n_rms = compute_rms(noise.data)

        if band == "broadband":
            results["noise_rms"] = float(f"{n_rms:.6e}")

        for phase, (win_start, win_end) in windows.items():
            try:
                signal = filt.slice(starttime=win_start, endtime=win_end)
            except Exception:
                results[f"snr_{phase}_{band}"] = np.nan
                results[f"snr_db_{phase}_{band}"] = np.nan
                continue

            if signal is None or len(signal.data) < 10:
                results[f"snr_{phase}_{band}"] = np.nan
                results[f"snr_db_{phase}_{band}"] = np.nan
                continue

            s_rms = compute_rms(signal.data)
            lin = snr_ratio(s_rms, n_rms)
            results[f"snr_{phase}_{band}"] = round(lin, 4)
            results[f"snr_db_{phase}_{band}"] = round(snr_db(lin), 2)

            if band == "broadband" and phase == "p":
                results["signal_rms_p"] = float(f"{s_rms:.6e}")
            elif band == "broadband" and phase == "s":
                results["signal_rms_s"] = float(f"{s_rms:.6e}")

    return results


# --- Main ---

def parse_args():
    p = argparse.ArgumentParser(description="Batch SNR computation")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--resume", action="store_true", help="Skip already-computed events")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between FDSN requests")
    p.add_argument("--skip-fdsn", action="store_true", help="Only use processed data")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)
    snr_cfg = cfg["snr"]
    freq_bands = {k: tuple(v) for k, v in snr_cfg["frequency_bands"].items()}

    # Load events
    events_file = Path(cfg["output_dir"]) / "data" / "batch_events.json"
    if not events_file.exists():
        print(f"No batch events at {events_file}. Run fetch_batch.py first.")
        return

    with open(events_file) as f:
        events = json.load(f)
    print(f"Loaded {len(events)} events")

    stations = load_stations(cfg["stations_file"])
    taup = TauPyModel(model=cfg["velocity_model"])
    cache_dir = Path(cfg["output_dir"]) / "cache"

    # Resume support
    out_csv = Path(cfg["output_dir"]) / "data" / "batch_snr_results.csv"
    done_keys = set()
    existing_rows = []
    if args.resume and out_csv.exists():
        existing = pd.read_csv(out_csv)
        done_keys = set(zip(existing["event_id"], existing["station"]))
        existing_rows = existing.to_dict("records")
        print(f"Resuming: {len(done_keys)} station-events already computed")

    all_results = list(existing_rows)
    total = len(events) * len(stations)
    n_done = len(done_keys)
    n_data = sum(1 for r in existing_rows
                 if not pd.isna(r.get("snr_db_broadband")))

    t0 = time.time()

    for i, event in enumerate(events):
        eid = event["event_id"]
        origin = UTCDateTime(event["origin_time"])
        ev_lat = event["latitude"]
        ev_lon = event["longitude"]
        ev_depth = event["depth_km"]

        print(f"\n[{i+1}/{len(events)}] {eid} M{event['magnitude']:.1f} "
              f"({event['distance_km']:.0f} km) — {event['origin_time'][:19]}")

        for station in stations:
            sta_name = station["station"]
            net = station["network"]

            if (eid, sta_name) in done_keys:
                continue

            dist_km, azimuth = station_distance_azimuth(station, ev_lat, ev_lon)
            p_travel, s_travel = get_arrivals(taup, ev_depth, dist_km)

            row = {
                "event_id": eid,
                "origin_time": event["origin_time"],
                "event_mag": event["magnitude"],
                "event_dist_km": event["distance_km"],
                "station": sta_name,
                "network": net,
                "model": station.get("model", "unknown"),
                "site_type": get_site_type(sta_name, cfg["site_types"]),
                "distance_km": round(dist_km, 2),
                "azimuth_deg": round(azimuth, 1),
                "p_travel_s": round(p_travel, 2) if p_travel else None,
                "s_travel_s": round(s_travel, 2) if s_travel else None,
            }

            if p_travel is None or s_travel is None:
                row["data_source"] = "none"
                for phase in ("p", "s", "combined"):
                    for band in freq_bands:
                        row[f"snr_{phase}_{band}"] = np.nan
                        row[f"snr_db_{phase}_{band}"] = np.nan
                row["noise_rms"] = np.nan
                row["signal_rms_p"] = np.nan
                row["signal_rms_s"] = np.nan
                all_results.append(row)
                n_done += 1
                continue

            noise_start = origin + p_travel - snr_cfg["noise_gap_sec"] - snr_cfg["noise_window_sec"]
            signal_end = origin + s_travel + snr_cfg["signal_after_s_sec"]

            # Try: cache → processed → FDSN
            st = load_cached(cache_dir, net, sta_name, eid)
            source = "cache"

            if st is None:
                st = load_processed(cfg["processed_dir"], net, sta_name,
                                    origin, noise_start, signal_end)
                source = "processed"

            if (st is None or len(st) == 0) and not args.skip_fdsn:
                st = fetch_fdsn(station, noise_start, signal_end,
                                cfg["fdsn_sources"], cfg["processing"], args.delay)
                source = "fdsn"
                if st is not None and len(st) > 0:
                    save_to_cache(st, cache_dir, net, sta_name, eid)

            if st is None or len(st) == 0:
                row["data_source"] = "none"
                for band in freq_bands:
                    row[f"snr_{band}"] = np.nan
                    row[f"snr_db_{band}"] = np.nan
                row["noise_rms"] = np.nan
                row["signal_rms"] = np.nan
                all_results.append(row)
                n_done += 1
                print(f"  {sta_name}: no data", flush=True)
                continue

            # Select vertical
            tr = None
            for ch in ["EHZ", "HHZ", "ENZ", "BHZ"]:
                sel = st.select(channel=ch)
                if len(sel) > 0:
                    tr = sel[0]
                    break
            if tr is None:
                tr = st[0]

            snr_results = measure_snr(tr, origin, p_travel, s_travel, snr_cfg, freq_bands)
            row["data_source"] = source
            row.update(snr_results)
            all_results.append(row)
            n_done += 1

            p_snr = snr_results.get("snr_db_p_broadband", np.nan)
            s_snr = snr_results.get("snr_db_s_broadband", np.nan)
            if not (np.isnan(p_snr) if isinstance(p_snr, float) else False):
                n_data += 1

            print(f"  {sta_name}: P={p_snr:.1f} S={s_snr:.1f} dB ({source})", flush=True)

        # Save checkpoint after each event
        df = pd.DataFrame(all_results)
        df.to_csv(out_csv, index=False)

    elapsed = time.time() - t0

    # Final summary
    df = pd.DataFrame(all_results)
    df.to_csv(out_csv, index=False)

    print(f"\n{'='*60}")
    print(f"Batch complete: {len(df)} measurements in {elapsed/60:.1f} min")
    print(f"  Events: {len(events)}")
    print(f"  Stations: {len(stations)}")
    print(f"  With data: {n_data}/{len(df)} ({100*n_data/max(len(df),1):.0f}%)")
    print(f"Saved to {out_csv}")

    # Per-station summary
    print(f"\n{'='*60}")
    print("Per-station mean SNR (dB):")
    print(f"{'station':>10} {'P-phase':>10} {'S-phase':>10} {'Combined':>10} {'n':>5}")
    print(f"{'='*60}")
    for sta in sorted(df["station"].unique()):
        sub = df[df["station"] == sta]
        p_vals = sub["snr_db_p_broadband"].dropna()
        s_vals = sub["snr_db_s_broadband"].dropna()
        c_vals = sub["snr_db_combined_broadband"].dropna()
        n = max(len(p_vals), len(s_vals))
        if n > 0:
            p_mean = f"{p_vals.mean():.1f}" if len(p_vals) > 0 else "—"
            s_mean = f"{s_vals.mean():.1f}" if len(s_vals) > 0 else "—"
            c_mean = f"{c_vals.mean():.1f}" if len(c_vals) > 0 else "—"
            print(f"  {sta:>8} {p_mean:>10} {s_mean:>10} {c_mean:>10} {n:>5}")
        else:
            print(f"  {sta:>8} {'—':>10} {'—':>10} {'—':>10} {'0':>5}")


if __name__ == "__main__":
    main()
