#!/usr/bin/env python3
"""
plot_results.py — Generate figures for SNR analysis.

Reads snr_results.csv and selected_events.json, produces:
  1. Waveform panels (one per scenario)
  2. SNR bar chart by station
  3. SNR vs distance scatter
  4. Detection quality map
  5. Spectral comparison (optional)
  6. Summary table

Usage:
    python plot_results.py
    python plot_results.py --config config.yaml --skip-waveforms
"""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd
from obspy import UTCDateTime, read
from obspy.taup import TauPyModel

from utils import load_config, load_stations, station_distance_azimuth

warnings.filterwarnings("ignore", message=".*No matching response.*")

# Style
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 10,
    "axes.linewidth": 0.8,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.grid": False,
})

SITE_COLORS = {
    "bedrock": "#2166ac",
    "basin": "#b2182b",
    "classroom": "#4dac26",
    "remote": "#7570b3",
    "unknown": "#666666",
}

SCENARIO_MARKERS = {
    "local": ("o", "#1b9e77"),
    "regional_low": ("s", "#d95f02"),
    "regional_high": ("D", "#7570b3"),
}


def save_fig(fig, name: str, output_dir: str, cfg: dict) -> None:
    """Save figure in configured formats."""
    fig_dir = Path(output_dir) / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    for fmt in cfg.get("figure_formats", ["png", "pdf"]):
        fig.savefig(fig_dir / f"{name}.{fmt}",
                    dpi=cfg.get("figure_dpi", 300),
                    bbox_inches="tight")
    print(f"  Saved {name}")


# ---------------------------------------------------------------
# Figure 1: Waveform panels
# ---------------------------------------------------------------

def plot_waveform_panel(
    scenario: str,
    event: dict,
    snr_df: pd.DataFrame,
    stations: list[dict],
    cfg: dict,
) -> None:
    """Plot stacked waveforms for one scenario, sorted by distance."""
    sub = snr_df[snr_df["scenario"] == scenario].copy()
    sub = sub.dropna(subset=["snr_db_broadband"])
    if len(sub) == 0:
        print(f"  No data for scenario '{scenario}', skipping waveform panel")
        return

    sub = sub.sort_values("distance_km")
    origin_time = UTCDateTime(event["origin_time"])

    taup_model = TauPyModel(model=cfg["velocity_model"])
    snr_cfg = cfg["snr"]

    n_traces = len(sub)
    fig, ax = plt.subplots(figsize=(12, max(4, n_traces * 0.8)))

    for i, (_, row) in enumerate(sub.iterrows()):
        sta_name = row["station"]
        net = row["network"]
        dist_km = row["distance_km"]

        # Find station info
        sta_info = next((s for s in stations if s["station"] == sta_name), None)
        if sta_info is None:
            continue

        # Load waveform
        p_travel = row.get("p_travel_s")
        s_travel = row.get("s_travel_s")
        if pd.isna(p_travel) or pd.isna(s_travel):
            continue

        noise_start = origin_time + p_travel - snr_cfg["noise_gap_sec"] - snr_cfg["noise_window_sec"]
        signal_end = origin_time + s_travel + snr_cfg["signal_after_s_sec"]

        # Try to load from processed dir
        from obspy import Stream
        st = Stream()
        for offset in range(-1, 2):
            t = origin_time + offset * 86400
            year = str(t.year)
            jday = f"{t.julday:03d}"
            day_dir = Path(cfg["processed_dir"]) / year / jday
            if day_dir.is_dir():
                for fp in day_dir.glob(f"{net}.{sta_name}.*.mseed"):
                    try:
                        st += read(str(fp))
                    except Exception:
                        continue

        if len(st) == 0:
            continue

        st.merge(method=1, fill_value="interpolate")

        # Select vertical
        tr = None
        for ch in ["EHZ", "HHZ", "ENZ"]:
            sel = st.select(channel=ch)
            if len(sel) > 0:
                tr = sel[0]
                break
        if tr is None:
            tr = st[0]

        # Filter and trim
        try:
            tr = tr.copy()
            tr.filter("bandpass", freqmin=1.0, freqmax=45.0, zerophase=True)
            tr.trim(starttime=noise_start - 5, endtime=signal_end + 5)
        except Exception:
            continue

        # Time axis relative to origin
        times = tr.times(reftime=origin_time)
        data = tr.data.copy()

        # Normalize
        peak = np.max(np.abs(data))
        if peak > 0:
            data = data / peak * 0.4

        # Plot trace
        color = SITE_COLORS.get(row.get("site_type", "unknown"), "#666")
        ax.plot(times, data + i, color=color, linewidth=0.5, alpha=0.8)

        # Mark P and S arrivals
        ax.axvline(p_travel, ymin=(i - 0.3) / n_traces, ymax=(i + 0.3) / n_traces,
                    color="red", linewidth=0.8, linestyle="--", alpha=0.7)
        ax.axvline(s_travel, ymin=(i - 0.3) / n_traces, ymax=(i + 0.3) / n_traces,
                    color="blue", linewidth=0.8, linestyle="--", alpha=0.7)

        # Label
        snr_val = row["snr_db_broadband"]
        label = f"{sta_name}  {dist_km:.0f} km  SNR={snr_val:.0f} dB"
        ax.text(times[0] - 1, i, label, fontsize=7, ha="right", va="center",
                color=color, fontweight="bold")

    ax.set_xlabel("Time relative to origin (s)")
    ax.set_yticks([])
    ax.set_title(
        f"{scenario.replace('_', ' ').title()}: "
        f"M{event['magnitude']:.2f} — {event['origin_time'][:19]}",
        fontsize=12, fontweight="bold",
    )

    # Legend
    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], color="red", linestyle="--", label="P arrival"),
               Line2D([0], [0], color="blue", linestyle="--", label="S arrival")]
    for stype, color in SITE_COLORS.items():
        if stype in [r.get("site_type") for _, r in sub.iterrows()]:
            handles.append(Line2D([0], [0], color=color, linewidth=2, label=stype))
    ax.legend(handles=handles, loc="upper right", fontsize=7, framealpha=0.9)

    save_fig(fig, f"fig1_waveforms_{scenario}", cfg["output_dir"], cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 2: SNR bar chart
# ---------------------------------------------------------------

def plot_snr_bars(snr_df: pd.DataFrame, cfg: dict) -> None:
    """Grouped bar chart of SNR by station and scenario."""
    scenarios = snr_df["scenario"].unique()
    stations_order = (
        snr_df.groupby("station")["snr_db_broadband"]
        .mean().sort_values(ascending=False).index.tolist()
    )

    n_scenarios = len(scenarios)
    n_stations = len(stations_order)
    bar_width = 0.8 / n_scenarios
    x = np.arange(n_stations)

    fig, ax = plt.subplots(figsize=(max(8, n_stations * 0.8), 5))

    for j, scenario in enumerate(scenarios):
        sub = snr_df[snr_df["scenario"] == scenario]
        values = []
        colors = []
        for sta in stations_order:
            row = sub[sub["station"] == sta]
            if len(row) > 0 and not pd.isna(row.iloc[0]["snr_db_broadband"]):
                values.append(row.iloc[0]["snr_db_broadband"])
                colors.append(SITE_COLORS.get(row.iloc[0].get("site_type", "unknown"), "#666"))
            else:
                values.append(0)
                colors.append("#cccccc")

        offset = (j - n_scenarios / 2 + 0.5) * bar_width
        _, marker_color = SCENARIO_MARKERS.get(scenario, ("o", "#333"))
        bars = ax.bar(x + offset, values, bar_width, label=scenario.replace("_", " ").title(),
                      color=marker_color, alpha=0.75, edgecolor="white", linewidth=0.5)

    # Threshold lines
    det_thresh = cfg["thresholds"]["detection_snr"]
    good_thresh = cfg["thresholds"]["good_snr"]
    det_db = 20 * np.log10(det_thresh) if det_thresh > 0 else 0
    good_db = 20 * np.log10(good_thresh) if good_thresh > 0 else 0

    ax.axhline(det_db, color="#e41a1c", linestyle="--", linewidth=0.8, alpha=0.7,
               label=f"Detection threshold ({det_db:.0f} dB)")
    ax.axhline(good_db, color="#4daf4a", linestyle="--", linewidth=0.8, alpha=0.7,
               label=f"Good quality ({good_db:.0f} dB)")

    ax.set_xticks(x)
    ax.set_xticklabels(stations_order, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("SNR (dB)")
    ax.set_title("Signal-to-Noise Ratio by Station", fontweight="bold")
    ax.legend(fontsize=7, loc="upper right")

    save_fig(fig, "fig2_snr_bars", cfg["output_dir"], cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 3: SNR vs distance
# ---------------------------------------------------------------

def plot_snr_vs_distance(snr_df: pd.DataFrame, cfg: dict) -> None:
    """Scatter plot of SNR vs epicentral distance."""
    fig, ax = plt.subplots(figsize=(9, 6))

    for scenario in snr_df["scenario"].unique():
        sub = snr_df[snr_df["scenario"] == scenario].dropna(subset=["snr_db_broadband"])
        marker, color = SCENARIO_MARKERS.get(scenario, ("o", "#333"))

        for _, row in sub.iterrows():
            site_color = SITE_COLORS.get(row.get("site_type", "unknown"), "#666")
            ax.scatter(row["distance_km"], row["snr_db_broadband"],
                       marker=marker, c=site_color, edgecolors=color,
                       linewidths=1.2, s=80, alpha=0.8, zorder=5)
            ax.annotate(row["station"], (row["distance_km"], row["snr_db_broadband"]),
                        fontsize=6, ha="left", va="bottom",
                        xytext=(3, 3), textcoords="offset points")

    # Geometric spreading reference (1/r)
    all_valid = snr_df.dropna(subset=["snr_db_broadband"])
    if len(all_valid) > 0:
        d_range = np.linspace(max(1, all_valid["distance_km"].min() * 0.5),
                              all_valid["distance_km"].max() * 1.2, 100)
        # Normalize: at median distance, use median SNR
        med_d = all_valid["distance_km"].median()
        med_snr = all_valid["snr_db_broadband"].median()
        ref_curve = med_snr - 20 * np.log10(d_range / med_d)
        ax.plot(d_range, ref_curve, "k--", linewidth=0.8, alpha=0.4, label="1/r decay")

    # Threshold lines
    det_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])
    good_db = 20 * np.log10(cfg["thresholds"]["good_snr"])
    ax.axhline(det_db, color="#e41a1c", linestyle=":", linewidth=0.7, alpha=0.5)
    ax.axhline(good_db, color="#4daf4a", linestyle=":", linewidth=0.7, alpha=0.5)

    ax.set_xlabel("Epicentral Distance (km)")
    ax.set_ylabel("SNR (dB)")
    ax.set_title("SNR vs. Distance", fontweight="bold")

    # Legend
    from matplotlib.lines import Line2D
    handles = []
    for scenario, (marker, color) in SCENARIO_MARKERS.items():
        if scenario in snr_df["scenario"].values:
            handles.append(Line2D([0], [0], marker=marker, color="none",
                                  markeredgecolor=color, markerfacecolor="gray",
                                  markersize=8, label=scenario.replace("_", " ").title()))
    for stype, color in SITE_COLORS.items():
        handles.append(Line2D([0], [0], marker="o", color="none",
                              markerfacecolor=color, markersize=8, label=stype))
    handles.append(Line2D([0], [0], linestyle="--", color="black", alpha=0.4, label="1/r decay"))
    ax.legend(handles=handles, fontsize=7, loc="upper right")

    save_fig(fig, "fig3_snr_vs_distance", cfg["output_dir"], cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 4: Detection quality map
# ---------------------------------------------------------------

def plot_detection_map(snr_df: pd.DataFrame, events: dict, cfg: dict) -> None:
    """Map stations colored by detection quality."""
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        has_cartopy = True
    except ImportError:
        has_cartopy = False
        print("  cartopy not installed, using plain map")

    scenarios = snr_df["scenario"].unique()
    n_panels = len(scenarios)

    fig, axes = plt.subplots(1, n_panels, figsize=(6 * n_panels, 6),
                              subplot_kw={"projection": ccrs.PlateCarree()} if has_cartopy else {})
    if n_panels == 1:
        axes = [axes]

    det_thresh_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])
    good_thresh_db = 20 * np.log10(cfg["thresholds"]["good_snr"])

    for ax, scenario in zip(axes, scenarios):
        sub = snr_df[snr_df["scenario"] == scenario]
        event = events.get(scenario, {})

        if has_cartopy:
            # Determine extent from stations + event
            all_lats = list(sub["latitude"]) if "latitude" in sub.columns else []
            all_lons = list(sub["longitude"]) if "longitude" in sub.columns else []

            # Use station positions from the main stations list
            stations = load_stations(cfg["stations_file"])
            sta_dict = {s["station"]: s for s in stations}
            lats = [sta_dict[s]["latitude"] for s in sub["station"] if s in sta_dict]
            lons = [sta_dict[s]["longitude"] for s in sub["station"] if s in sta_dict]

            if event:
                lats.append(event["latitude"])
                lons.append(event["longitude"])

            if lats and lons:
                pad = 0.3
                ax.set_extent([min(lons) - pad, max(lons) + pad,
                               min(lats) - pad, max(lats) + pad])

            ax.add_feature(cfeature.BORDERS, linewidth=0.5, alpha=0.5)
            ax.add_feature(cfeature.STATES, linewidth=0.3, alpha=0.3)
            ax.coastlines(resolution="50m", linewidth=0.3, alpha=0.3)

        # Plot stations
        for _, row in sub.iterrows():
            sta_name = row["station"]
            sta_info = sta_dict.get(sta_name)
            if sta_info is None:
                continue

            snr_val = row.get("snr_db_broadband", np.nan)
            if pd.isna(snr_val):
                color = "#999999"
                marker_size = 60
            elif snr_val >= good_thresh_db:
                color = "#2ca02c"  # green
                marker_size = 100
            elif snr_val >= det_thresh_db:
                color = "#ff7f0e"  # yellow/orange
                marker_size = 80
            else:
                color = "#d62728"  # red
                marker_size = 60

            ax.scatter(sta_info["longitude"], sta_info["latitude"],
                       c=color, s=marker_size, edgecolors="black",
                       linewidths=0.5, zorder=10,
                       transform=ccrs.PlateCarree() if has_cartopy else None)
            ax.annotate(sta_name,
                        (sta_info["longitude"], sta_info["latitude"]),
                        fontsize=5, ha="left", va="bottom",
                        xytext=(4, 4), textcoords="offset points")

        # Plot event
        if event:
            ax.scatter(event["longitude"], event["latitude"],
                       marker="*", c="gold", s=200, edgecolors="black",
                       linewidths=0.8, zorder=15,
                       transform=ccrs.PlateCarree() if has_cartopy else None)

        ax.set_title(f"{scenario.replace('_', ' ').title()}\n"
                     f"M{event.get('magnitude', '?')}", fontsize=10)

    # Legend
    from matplotlib.lines import Line2D
    handles = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#2ca02c",
               markeredgecolor="black", markersize=10, label=f"SNR > {good_thresh_db:.0f} dB (good)"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#ff7f0e",
               markeredgecolor="black", markersize=8, label=f"SNR {det_thresh_db:.0f}–{good_thresh_db:.0f} dB (marginal)"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#d62728",
               markeredgecolor="black", markersize=8, label=f"SNR < {det_thresh_db:.0f} dB (poor)"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#999999",
               markeredgecolor="black", markersize=8, label="No data"),
        Line2D([0], [0], marker="*", color="none", markerfacecolor="gold",
               markeredgecolor="black", markersize=12, label="Event"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=5, fontsize=7,
               bbox_to_anchor=(0.5, -0.02))

    fig.suptitle("Detection Quality Map", fontsize=13, fontweight="bold", y=1.02)
    save_fig(fig, "fig4_detection_map", cfg["output_dir"], cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 5: Spectral comparison
# ---------------------------------------------------------------

def plot_spectral_comparison(snr_df: pd.DataFrame, events: dict,
                              stations: list[dict], cfg: dict) -> None:
    """Frequency spectra for best, worst RS stations and broadband reference."""
    # Use regional_low if available, else local
    scenario = "regional_low" if "regional_low" in events else "local"
    if scenario not in events:
        print("  No event for spectral comparison, skipping")
        return

    event = events[scenario]
    sub = snr_df[(snr_df["scenario"] == scenario)].dropna(subset=["snr_db_broadband"])

    # Split RS vs broadband
    rs_stations = sub[sub["model"].isin(["RS3D", "RS4D"])]
    bb_stations = sub[sub["station"] == "KIDD"]

    if len(rs_stations) == 0:
        print("  No RS stations with data for spectral comparison, skipping")
        return

    best_rs = rs_stations.loc[rs_stations["snr_db_broadband"].idxmax()]
    worst_rs = rs_stations.loc[rs_stations["snr_db_broadband"].idxmin()]

    compare_stations = [
        (best_rs["station"], best_rs["network"], f"Best RS ({best_rs['station']})", "#2166ac"),
        (worst_rs["station"], worst_rs["network"], f"Worst RS ({worst_rs['station']})", "#b2182b"),
    ]
    if len(bb_stations) > 0:
        bb = bb_stations.iloc[0]
        compare_stations.append((bb["station"], bb["network"], "Broadband (KIDD)", "#4dac26"))

    origin_time = UTCDateTime(event["origin_time"])
    taup_model = TauPyModel(model=cfg["velocity_model"])
    snr_cfg = cfg["snr"]

    fig, ax = plt.subplots(figsize=(10, 6))
    sta_dict = {s["station"]: s for s in stations}

    for sta_name, net, label, color in compare_stations:
        sta_info = sta_dict.get(sta_name)
        if sta_info is None:
            continue

        dist_km, _ = station_distance_azimuth(sta_info, event["latitude"], event["longitude"])

        # Get travel times
        from utils import compute_rms
        dist_deg = dist_km / 111.19
        try:
            p_arr = taup_model.get_travel_times(
                source_depth_in_km=max(event["depth_km"], 0.01),
                distance_in_degree=dist_deg, phase_list=["P", "p", "Pn", "Pg"])
            s_arr = taup_model.get_travel_times(
                source_depth_in_km=max(event["depth_km"], 0.01),
                distance_in_degree=dist_deg, phase_list=["S", "s", "Sn", "Sg"])
            p_travel = p_arr[0].time if p_arr else None
            s_travel = s_arr[0].time if s_arr else None
        except Exception:
            continue

        if p_travel is None or s_travel is None:
            continue

        # Load waveform
        from obspy import Stream
        st = Stream()
        for offset in range(-1, 2):
            t = origin_time + offset * 86400
            year = str(t.year)
            jday = f"{t.julday:03d}"
            day_dir = Path(cfg["processed_dir"]) / year / jday
            if day_dir.is_dir():
                for fp in day_dir.glob(f"{net}.{sta_name}.*.mseed"):
                    try:
                        st += read(str(fp))
                    except Exception:
                        continue

        if len(st) == 0:
            continue

        st.merge(method=1, fill_value="interpolate")
        tr = None
        for ch in ["EHZ", "HHZ"]:
            sel = st.select(channel=ch)
            if len(sel) > 0:
                tr = sel[0]
                break
        if tr is None:
            continue

        # Signal window spectrum
        signal_start = origin_time + p_travel
        signal_end = origin_time + s_travel + snr_cfg["signal_after_s_sec"]
        try:
            sig_tr = tr.copy().slice(starttime=signal_start, endtime=signal_end)
        except Exception:
            continue

        if sig_tr is None or len(sig_tr.data) < 64:
            continue

        nfft = 2 ** int(np.ceil(np.log2(len(sig_tr.data))))
        freqs = np.fft.rfftfreq(nfft, d=1.0 / sig_tr.stats.sampling_rate)
        spectrum = np.abs(np.fft.rfft(sig_tr.data, n=nfft))
        spectrum = spectrum / len(sig_tr.data)

        # Smooth with running mean
        kernel = 5
        if len(spectrum) > kernel:
            spectrum = np.convolve(spectrum, np.ones(kernel) / kernel, mode="same")

        mask = (freqs >= 0.5) & (freqs <= 48)
        ax.loglog(freqs[mask], spectrum[mask], color=color, linewidth=1.5,
                  label=label, alpha=0.85)

        # Noise window spectrum
        noise_end = origin_time + p_travel - snr_cfg["noise_gap_sec"]
        noise_start = noise_end - snr_cfg["noise_window_sec"]
        try:
            noise_tr = tr.copy().slice(starttime=noise_start, endtime=noise_end)
            if noise_tr and len(noise_tr.data) >= 64:
                n_spectrum = np.abs(np.fft.rfft(noise_tr.data, n=nfft))
                n_spectrum = n_spectrum / len(noise_tr.data)
                if len(n_spectrum) > kernel:
                    n_spectrum = np.convolve(n_spectrum, np.ones(kernel) / kernel, mode="same")
                ax.loglog(freqs[mask], n_spectrum[mask], color=color, linewidth=0.7,
                          linestyle=":", alpha=0.5)
        except Exception:
            pass

    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Amplitude Spectrum")
    ax.set_title(f"Spectral Comparison — {scenario.replace('_', ' ').title()} "
                 f"(M{event['magnitude']:.2f})", fontweight="bold")
    ax.set_xlim(0.5, 48)
    ax.legend(fontsize=8)
    ax.text(0.02, 0.02, "Solid = signal, dotted = noise", transform=ax.transAxes,
            fontsize=7, color="gray")

    save_fig(fig, "fig5_spectral", cfg["output_dir"], cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------

def generate_summary(snr_df: pd.DataFrame, cfg: dict) -> None:
    """Generate summary CSV with recommendations."""
    stations = snr_df["station"].unique()
    det_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])
    good_db = 20 * np.log10(cfg["thresholds"]["good_snr"])

    rows = []
    for sta in stations:
        sub = snr_df[snr_df["station"] == sta]
        first = sub.iloc[0]

        snr_vals = sub["snr_db_broadband"].dropna()
        avg_snr = snr_vals.mean() if len(snr_vals) > 0 else np.nan
        n_detected = (snr_vals >= det_db).sum() if len(snr_vals) > 0 else 0
        n_scenarios = len(sub)

        if pd.isna(avg_snr):
            rec = "no data"
        elif avg_snr >= good_db:
            rec = "good"
        elif avg_snr >= det_db:
            rec = "marginal"
        else:
            rec = "needs attention"

        row = {
            "station": sta,
            "network": first["network"],
            "model": first["model"],
            "site_type": first.get("site_type", "unknown"),
        }

        # Per-scenario SNR
        for scenario in snr_df["scenario"].unique():
            sc_row = sub[sub["scenario"] == scenario]
            if len(sc_row) > 0:
                row[f"snr_db_{scenario}"] = sc_row.iloc[0].get("snr_db_broadband", np.nan)
            else:
                row[f"snr_db_{scenario}"] = np.nan

        row["avg_snr_db"] = round(avg_snr, 1) if not pd.isna(avg_snr) else np.nan
        row["detection_rate"] = f"{n_detected}/{n_scenarios}"
        row["recommendation"] = rec
        rows.append(row)

    summary = pd.DataFrame(rows)
    summary = summary.sort_values("avg_snr_db", ascending=False, na_position="last")

    out_csv = Path(cfg["output_dir"]) / "data" / "snr_summary.csv"
    summary.to_csv(out_csv, index=False)
    print(f"\nSummary saved to {out_csv}")

    # Print
    print(f"\n{'='*80}")
    print("Station SNR Summary")
    print(f"{'='*80}")
    print(summary.to_string(index=False))
    print(f"\nRecommendations: good={good_db:.0f}+ dB, "
          f"marginal={det_db:.0f}–{good_db:.0f} dB, "
          f"needs attention=<{det_db:.0f} dB")


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate SNR analysis figures")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--skip-waveforms", action="store_true",
                        help="Skip waveform panel generation (slow)")
    parser.add_argument("--skip-map", action="store_true",
                        help="Skip map generation (requires cartopy)")
    parser.add_argument("--skip-spectral", action="store_true",
                        help="Skip spectral comparison")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)

    # Load results
    results_csv = Path(cfg["output_dir"]) / "data" / "snr_results.csv"
    if not results_csv.exists():
        print(f"No results found at {results_csv}. Run compute_snr.py first.")
        return

    snr_df = pd.read_csv(results_csv)
    print(f"Loaded {len(snr_df)} measurements from {results_csv}")

    # Load events
    events_file = Path(cfg["output_dir"]) / "data" / "selected_events.json"
    with open(events_file) as f:
        events = json.load(f)

    stations = load_stations(cfg["stations_file"])

    # Generate figures
    print("\nGenerating figures...")

    if not args.skip_waveforms:
        for scenario in snr_df["scenario"].unique():
            if scenario in events:
                plot_waveform_panel(scenario, events[scenario], snr_df, stations, cfg)

    plot_snr_bars(snr_df, cfg)
    plot_snr_vs_distance(snr_df, cfg)

    if not args.skip_map:
        plot_detection_map(snr_df, events, cfg)

    if not args.skip_spectral:
        plot_spectral_comparison(snr_df, events, stations, cfg)

    generate_summary(snr_df, cfg)

    print("\nDone.")


if __name__ == "__main__":
    main()
