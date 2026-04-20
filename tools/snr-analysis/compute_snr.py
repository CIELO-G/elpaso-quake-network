#!/usr/bin/env python3
"""
compute_snr.py — Compute SNR for each station/event scenario.

Loads selected events from fetch_events.py output, retrieves waveforms
(from pipeline processed data or via FDSN), computes theoretical arrivals,
and measures SNR in multiple frequency bands.

Usage:
    python compute_snr.py
    python compute_snr.py --config config.yaml
"""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from obspy import UTCDateTime, Stream, read
from obspy.clients.fdsn import Client
from obspy.taup import TauPyModel

from utils import (
    load_config, load_stations, station_distance_azimuth,
    get_site_type, compute_rms, snr_ratio, snr_db,
)

# Suppress ObsPy warnings about missing response info
warnings.filterwarnings("ignore", message=".*No matching response.*")


def get_theoretical_arrivals(
    model: TauPyModel,
    ev_depth_km: float,
    distance_km: float,
) -> tuple[float | None, float | None]:
    """Return (P_travel_time_sec, S_travel_time_sec) or None if not found."""
    distance_deg = distance_km / 111.19
    try:
        arrivals = model.get_travel_times(
            source_depth_in_km=max(ev_depth_km, 0.01),
            distance_in_degree=distance_deg,
            phase_list=["P", "p", "Pn", "Pg"],
        )
        p_time = arrivals[0].time if arrivals else None
    except Exception:
        p_time = None

    try:
        arrivals = model.get_travel_times(
            source_depth_in_km=max(ev_depth_km, 0.01),
            distance_in_degree=distance_deg,
            phase_list=["S", "s", "Sn", "Sg"],
        )
        s_time = arrivals[0].time if arrivals else None
    except Exception:
        s_time = None

    return p_time, s_time


def load_processed_waveform(
    processed_dir: str,
    station: dict,
    origin_time: UTCDateTime,
    p_travel: float,
    s_travel: float,
    snr_cfg: dict,
) -> Stream | None:
    """Try to load waveform from pipeline processed data."""
    # Determine which day(s) to look at
    noise_start = origin_time + p_travel - snr_cfg["noise_gap_sec"] - snr_cfg["noise_window_sec"]
    signal_end = origin_time + s_travel + snr_cfg["signal_after_s_sec"]

    net = station["network"]
    sta = station["station"]

    st = Stream()
    # Check the day of the origin time and surrounding days
    for offset in range(-1, 2):
        t = origin_time + offset * 86400
        year = str(t.year)
        jday = f"{t.julday:03d}"
        day_dir = Path(processed_dir) / year / jday

        if not day_dir.is_dir():
            continue

        # Look for this station's files
        for pattern in [f"{net}.{sta}.*.mseed", f"{net}.{sta}.*.*.*.mseed"]:
            for fp in day_dir.glob(pattern):
                try:
                    st += read(str(fp))
                except Exception:
                    continue

    if len(st) == 0:
        return None

    # Trim to the analysis window (with some buffer)
    try:
        st.merge(method=1, fill_value="interpolate")
        st.trim(starttime=noise_start - 5, endtime=signal_end + 5)
    except Exception:
        pass

    return st if len(st) > 0 else None


def fetch_fdsn_waveform(
    station: dict,
    origin_time: UTCDateTime,
    p_travel: float,
    s_travel: float,
    snr_cfg: dict,
    fdsn_sources: dict,
    proc_cfg: dict,
) -> Stream | None:
    """Fetch waveform via FDSN and process it."""
    net = station["network"]
    sta = station["station"]
    source = fdsn_sources.get(net)
    if not source:
        return None

    noise_start = origin_time + p_travel - snr_cfg["noise_gap_sec"] - snr_cfg["noise_window_sec"]
    signal_end = origin_time + s_travel + snr_cfg["signal_after_s_sec"]

    # Determine channel
    model = station.get("model", "")
    if model == "RS4D":
        chan = "EHZ"
    elif station.get("channels") == "HH?":
        chan = "HH*"
    else:
        chan = "EH*"

    loc = station.get("location", "00")

    try:
        client = Client(source)
        st = client.get_waveforms(
            network=net, station=sta, location=loc, channel=chan,
            starttime=noise_start - 30, endtime=signal_end + 30,
        )
    except Exception:
        return None

    if len(st) == 0:
        return None

    # Process: remove response, filter
    try:
        st.merge(method=1, fill_value="interpolate")
        st.detrend("demean")
        st.detrend("linear")
        st.taper(max_percentage=0.05, type="hann")

        # Try to remove instrument response
        inv = client.get_stations(
            network=net, station=sta, location=loc, channel=chan,
            starttime=noise_start, endtime=signal_end,
            level="response",
        )
        pre_filt = tuple(proc_cfg["pre_filt"])
        st.remove_response(
            inventory=inv,
            output=proc_cfg["output"],
            pre_filt=pre_filt,
            water_level=proc_cfg["water_level"],
        )
    except Exception:
        # If response removal fails, just use raw counts with basic filtering
        try:
            st.detrend("demean")
            st.filter("bandpass",
                       freqmin=proc_cfg["filter_freqmin"],
                       freqmax=proc_cfg["filter_freqmax"],
                       zerophase=True)
        except Exception:
            pass

    return st if len(st) > 0 else None


def compute_station_snr(
    tr,
    origin_time: UTCDateTime,
    p_travel: float,
    s_travel: float,
    snr_cfg: dict,
    freq_bands: dict,
) -> dict:
    """Compute SNR in multiple frequency bands for a single trace."""
    results = {}

    p_abs = origin_time + p_travel
    s_abs = origin_time + s_travel

    noise_end = p_abs - snr_cfg["noise_gap_sec"]
    noise_start = noise_end - snr_cfg["noise_window_sec"]
    signal_start = p_abs
    signal_end = s_abs + snr_cfg["signal_after_s_sec"]

    for band_name, (fmin, fmax) in freq_bands.items():
        try:
            tr_filt = tr.copy()
            tr_filt.filter("bandpass", freqmin=fmin, freqmax=fmax, zerophase=True)
        except Exception:
            results[f"snr_{band_name}"] = np.nan
            results[f"snr_db_{band_name}"] = np.nan
            continue

        # Extract windows
        try:
            noise_slice = tr_filt.slice(starttime=noise_start, endtime=noise_end)
            signal_slice = tr_filt.slice(starttime=signal_start, endtime=signal_end)
        except Exception:
            results[f"snr_{band_name}"] = np.nan
            results[f"snr_db_{band_name}"] = np.nan
            continue

        if noise_slice is None or signal_slice is None:
            results[f"snr_{band_name}"] = np.nan
            results[f"snr_db_{band_name}"] = np.nan
            continue

        if len(noise_slice.data) < 10 or len(signal_slice.data) < 10:
            results[f"snr_{band_name}"] = np.nan
            results[f"snr_db_{band_name}"] = np.nan
            continue

        noise_rms = compute_rms(noise_slice.data)
        signal_rms = compute_rms(signal_slice.data)
        snr_lin = snr_ratio(signal_rms, noise_rms)
        snr_decibel = snr_db(snr_lin)

        results[f"snr_{band_name}"] = round(snr_lin, 4)
        results[f"snr_db_{band_name}"] = round(snr_decibel, 2)

        # Store broadband RMS values
        if band_name == "broadband":
            results["noise_rms"] = float(f"{noise_rms:.6e}")
            results["signal_rms"] = float(f"{signal_rms:.6e}")

    return results


def check_detection(
    assignments_file: str,
    event_id: str,
    station_name: str,
) -> tuple[bool, bool]:
    """Check if P and S were detected for a local event."""
    try:
        df = pd.read_csv(assignments_file)
        sta_picks = df[(df["event_id"] == event_id) & (df["station"] == station_name)]
        p_detected = any(sta_picks["phase"] == "P")
        s_detected = any(sta_picks["phase"] == "S")
        return p_detected, s_detected
    except Exception:
        return False, False


def process_scenario(
    scenario: str,
    event: dict,
    stations: list[dict],
    cfg: dict,
    taup_model: TauPyModel,
) -> list[dict]:
    """Process one event scenario across all stations."""
    origin_time = UTCDateTime(event["origin_time"])
    ev_lat = event["latitude"]
    ev_lon = event["longitude"]
    ev_depth = event["depth_km"]

    snr_cfg = cfg["snr"]
    freq_bands = {k: tuple(v) for k, v in snr_cfg["frequency_bands"].items()}

    results = []

    for station in stations:
        sta_name = station["station"]
        net = station["network"]
        model_name = station.get("model", "unknown")

        dist_km, azimuth = station_distance_azimuth(station, ev_lat, ev_lon)

        print(f"  {net}.{sta_name} — {dist_km:.1f} km ... ", end="", flush=True)

        # Theoretical arrivals
        p_travel, s_travel = get_theoretical_arrivals(taup_model, ev_depth, dist_km)
        if p_travel is None or s_travel is None:
            print("no arrivals predicted, skipping")
            results.append({
                "event_id": event.get("event_id", ""),
                "scenario": scenario,
                "station": sta_name,
                "network": net,
                "model": model_name,
                "site_type": get_site_type(sta_name, cfg["site_types"]),
                "distance_km": round(dist_km, 2),
                "azimuth_deg": round(azimuth, 1),
                "p_travel_s": None,
                "s_travel_s": None,
            })
            continue

        # Try processed data first, then FDSN
        st = load_processed_waveform(
            cfg["processed_dir"], station, origin_time,
            p_travel, s_travel, snr_cfg,
        )
        data_source = "processed"

        if st is None or len(st) == 0:
            st = fetch_fdsn_waveform(
                station, origin_time, p_travel, s_travel,
                snr_cfg, cfg["fdsn_sources"], cfg["processing"],
            )
            data_source = "fdsn"

        if st is None or len(st) == 0:
            print("no data")
            row = {
                "event_id": event.get("event_id", ""),
                "scenario": scenario,
                "station": sta_name,
                "network": net,
                "model": model_name,
                "site_type": get_site_type(sta_name, cfg["site_types"]),
                "distance_km": round(dist_km, 2),
                "azimuth_deg": round(azimuth, 1),
                "p_travel_s": round(p_travel, 2),
                "s_travel_s": round(s_travel, 2),
                "data_source": "none",
            }
            for band in freq_bands:
                row[f"snr_{band}"] = np.nan
                row[f"snr_db_{band}"] = np.nan
            row["noise_rms"] = np.nan
            row["signal_rms"] = np.nan
            row["p_detected"] = False
            row["s_detected"] = False
            results.append(row)
            continue

        # Select vertical component
        tr = None
        for ch_code in ["EHZ", "HHZ", "ENZ", "BHZ"]:
            sel = st.select(channel=ch_code)
            if len(sel) > 0:
                tr = sel[0]
                break
        if tr is None:
            tr = st[0]  # fallback to first trace

        # Compute SNR
        snr_results = compute_station_snr(
            tr, origin_time, p_travel, s_travel, snr_cfg, freq_bands,
        )

        # Check pipeline detections (only for local events)
        p_det, s_det = False, False
        if scenario == "local" and "event_id" in event and event["event_id"]:
            p_det, s_det = check_detection(
                cfg["assignments_file"], event["event_id"], sta_name,
            )

        row = {
            "event_id": event.get("event_id", ""),
            "scenario": scenario,
            "station": sta_name,
            "network": net,
            "model": model_name,
            "site_type": get_site_type(sta_name, cfg["site_types"]),
            "distance_km": round(dist_km, 2),
            "azimuth_deg": round(azimuth, 1),
            "p_travel_s": round(p_travel, 2),
            "s_travel_s": round(s_travel, 2),
            "data_source": data_source,
            **snr_results,
            "p_detected": p_det,
            "s_detected": s_det,
        }
        results.append(row)

        snr_val = snr_results.get("snr_db_broadband", np.nan)
        print(f"SNR={snr_val:.1f} dB ({data_source})")

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute SNR for selected events")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)

    # Load selected events
    events_file = Path(cfg["output_dir"]) / "data" / "selected_events.json"
    if not events_file.exists():
        print(f"No selected events found at {events_file}.")
        print("Run fetch_events.py first.")
        return

    with open(events_file) as f:
        selected = json.load(f)

    if not selected:
        print("No events selected. Run fetch_events.py to pick events.")
        return

    stations = load_stations(cfg["stations_file"])
    taup_model = TauPyModel(model=cfg["velocity_model"])

    all_results = []

    for scenario, event in selected.items():
        print(f"\n{'='*60}")
        print(f"Scenario: {scenario}")
        print(f"  Event: {event.get('event_id', 'N/A')} | "
              f"M{event['magnitude']:.2f} | {event['origin_time']}")
        print(f"  Location: {event['latitude']:.4f}, {event['longitude']:.4f}, "
              f"depth={event['depth_km']:.1f} km")
        print(f"{'='*60}")

        results = process_scenario(scenario, event, stations, cfg, taup_model)
        all_results.extend(results)

    # Save results
    df = pd.DataFrame(all_results)
    out_csv = Path(cfg["output_dir"]) / "data" / "snr_results.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)
    print(f"\nResults saved to {out_csv}")
    print(f"Total: {len(df)} station-event measurements")

    # Quick summary
    print(f"\n{'='*60}")
    print("Summary (broadband SNR dB by scenario):")
    print(f"{'='*60}")
    for scenario in df["scenario"].unique():
        sub = df[df["scenario"] == scenario]
        valid = sub["snr_db_broadband"].dropna()
        if len(valid) > 0:
            print(f"\n  {scenario}:")
            print(f"    Stations with data: {len(valid)}/{len(sub)}")
            print(f"    SNR range: {valid.min():.1f} — {valid.max():.1f} dB")
            print(f"    Mean: {valid.mean():.1f} dB | Median: {valid.median():.1f} dB")


if __name__ == "__main__":
    main()
