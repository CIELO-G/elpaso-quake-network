#!/usr/bin/env python3
"""
plot_waveforms.py — Cherry-picked waveform panels for poster/publication.

Plots stacked waveforms for selected events, sorted by distance,
with P/S arrival markers and SNR labels. Color-coded by site type.

Usage:
    python plot_waveforms.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from obspy import UTCDateTime, Stream, read
from obspy.taup import TauPyModel

from utils import load_config, load_stations, station_distance_azimuth, get_site_type

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 9,
    "axes.linewidth": 0.6,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})

SITE_COLORS = {
    "bedrock": "#2166ac",
    "basin": "#b2182b",
}

# Three cherry-picked events: strong, moderate, weak
EVENTS = [
    {
        "origin_time": "2026-03-07T07:11:26",
        "latitude": 31.676,
        "longitude": -104.367,
        "depth_km": 6.1,
        "magnitude": 3.9,
        "label": "M3.9 — Strong Signal",
    },
    {
        "origin_time": "2026-02-16T07:30:43",
        "latitude": 31.551,
        "longitude": -104.268,
        "depth_km": 7.6,
        "magnitude": 3.1,
        "label": "M3.1 — Moderate Signal",
    },
    {
        "origin_time": "2026-03-30T15:50:11",
        "latitude": 31.517,
        "longitude": -104.425,
        "depth_km": 7.0,
        "magnitude": 2.7,
        "label": "M2.7 — Near Detection Limit",
    },
]


def load_waveform(processed_dir, net, sta, origin, t_start, t_end):
    """Load processed waveform for a station."""
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


def main():
    cfg = load_config("config.yaml")
    stations = load_stations(cfg["stations_file"])
    taup = TauPyModel(model=cfg["velocity_model"])
    snr_cfg = cfg["snr"]
    exclude = cfg.get("exclude_stations", [])

    # Load batch SNR results for labels
    snr_df = pd.read_csv(Path(cfg["output_dir"]) / "data" / "batch_snr_results.csv")

    # Filter stations
    stations = [s for s in stations if s["station"] not in exclude]

    n_events = len(EVENTS)
    fig, axes = plt.subplots(1, n_events, figsize=(5.5 * n_events, 7), sharey=False)
    if n_events == 1:
        axes = [axes]

    for col, (ax, event) in enumerate(zip(axes, EVENTS)):
        origin = UTCDateTime(event["origin_time"])
        ev_lat = event["latitude"]
        ev_lon = event["longitude"]
        ev_depth = event["depth_km"]

        # Get distances and sort
        sta_info = []
        for s in stations:
            dist_km, _ = station_distance_azimuth(s, ev_lat, ev_lon)
            stype = get_site_type(s["station"], cfg["site_types"])
            sta_info.append((s, dist_km, stype))
        sta_info.sort(key=lambda x: x[1])

        n_traces = len(sta_info)
        trace_idx = 0

        for s, dist_km, stype in sta_info:
            net = s["network"]
            sta_name = s["station"]
            color = SITE_COLORS.get(stype, "#999")

            # Travel times
            dist_deg = dist_km / 111.19
            try:
                p_arr = taup.get_travel_times(
                    source_depth_in_km=max(ev_depth, 0.01),
                    distance_in_degree=dist_deg,
                    phase_list=["P", "p", "Pn", "Pg"])
                s_arr = taup.get_travel_times(
                    source_depth_in_km=max(ev_depth, 0.01),
                    distance_in_degree=dist_deg,
                    phase_list=["S", "s", "Sn", "Sg"])
                p_travel = p_arr[0].time if p_arr else None
                s_travel = s_arr[0].time if s_arr else None
            except Exception:
                p_travel, s_travel = None, None

            if p_travel is None or s_travel is None:
                trace_idx += 1
                continue

            noise_start = origin + p_travel - snr_cfg["noise_gap_sec"] - snr_cfg["noise_window_sec"]
            signal_end = origin + s_travel + snr_cfg["signal_after_s_sec"]

            st = load_waveform(cfg["processed_dir"], net, sta_name,
                               origin, noise_start, signal_end)
            if st is None:
                trace_idx += 1
                continue

            # Select vertical
            tr = None
            for ch in ["EHZ", "HHZ", "ENZ"]:
                sel = st.select(channel=ch)
                if len(sel) > 0:
                    tr = sel[0]
                    break
            if tr is None:
                trace_idx += 1
                continue

            tr = tr.copy()
            try:
                tr.filter("bandpass", freqmin=3.0, freqmax=45.0, zerophase=True)
                tr.trim(starttime=noise_start, endtime=signal_end)
            except Exception:
                trace_idx += 1
                continue

            # Time axis relative to origin
            times = tr.times(reftime=origin)
            data = tr.data.copy().astype(float)

            # Normalize per trace
            peak = np.max(np.abs(data))
            if peak > 0:
                data = data / peak * 0.38

            # Plot
            y_offset = trace_idx
            ax.plot(times, data + y_offset, color=color, linewidth=0.5, alpha=0.85)

            # P and S vertical lines (shifted +1s to align with observed arrivals)
            ax.vlines(p_travel + 1, y_offset - 0.4, y_offset + 0.4,
                      color="#e41a1c", linewidth=0.7, linestyle="--", alpha=0.7, zorder=10)
            ax.vlines(s_travel + 1, y_offset - 0.4, y_offset + 0.4,
                      color="#4575b4", linewidth=0.7, linestyle="--", alpha=0.7, zorder=10)

            # Get SNR from batch results
            snr_row = snr_df[(snr_df["origin_time"].str[:19] == event["origin_time"][:19]) &
                             (snr_df["station"] == sta_name)]
            p_snr = s_snr = "—"
            if len(snr_row) > 0:
                p_val = snr_row.iloc[0].get("snr_db_p_broadband")
                s_val = snr_row.iloc[0].get("snr_db_s_broadband")
                if not pd.isna(p_val):
                    p_snr = f"{p_val:.0f}"
                if not pd.isna(s_val):
                    s_snr = f"{s_val:.0f}"

            # Label: station name + distance (outside left edge)
            label = f"{sta_name}  {dist_km:.0f} km"
            ax.annotate(label, xy=(0, y_offset), xycoords=("axes fraction", "data"),
                        xytext=(-5, 0), textcoords="offset points",
                        fontsize=6.5, ha="right", va="center",
                        color=color, fontweight="bold")

            # SNR above the trace
            snr_label = f"P:{p_snr}  S:{s_snr} dB"
            ax.text(times[0] + (times[-1] - times[0]) * 0.02, y_offset + 0.35,
                    snr_label, fontsize=5, ha="left", va="bottom", color="#555")

            trace_idx += 1

        ax.set_yticks([])
        ax.set_xlabel("Time relative to origin (s)", fontsize=8)
        ax.set_title(f"{event['label']}\n{event['origin_time'][:19]} UTC  "
                     f"({event.get('depth_km', '?')} km depth)",
                     fontsize=9.5, fontweight="bold", pad=10)

        # Trim x-axis padding
        ax.margins(x=0.02)

    # Shared legend
    handles = [
        Line2D([0], [0], color=SITE_COLORS["bedrock"], linewidth=2, label="Bedrock"),
        Line2D([0], [0], color=SITE_COLORS["basin"], linewidth=2, label="Basin"),
        Line2D([0], [0], color="#e41a1c", linewidth=0.7, linestyle="--",
               alpha=0.7, label="P arrival"),
        Line2D([0], [0], color="#4575b4", linewidth=0.7, linestyle="--",
               alpha=0.7, label="S arrival"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=8,
               bbox_to_anchor=(0.5, -0.02), framealpha=0.9)

    fig.suptitle("Waveform Examples — Regional Seismicity Recorded by El Paso Network",
                 fontsize=13, fontweight="bold", y=1.01)
    fig.subplots_adjust(left=0.10, right=0.97, wspace=0.30)

    # Save
    fig_dir = Path(cfg["output_dir"]) / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    for fmt in cfg.get("figure_formats", ["png", "pdf"]):
        fig.savefig(fig_dir / f"fig_waveform_examples.{fmt}",
                    dpi=cfg.get("figure_dpi", 300), bbox_inches="tight")
    print("Saved fig_waveform_examples")
    plt.close(fig)


if __name__ == "__main__":
    main()
