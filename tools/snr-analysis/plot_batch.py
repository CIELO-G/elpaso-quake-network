#!/usr/bin/env python3
"""
plot_batch.py — Publication-quality figures for batch SNR analysis.

Reads batch_snr_results.csv and batch_events.json, produces 4 figures:
  1. SNR vs distance (P and S panels, colored by magnitude)
  2. Station box plots grouped by site type
  3. Detection map with event cloud
  4. Frequency-band comparison per station

Usage:
    python plot_batch.py
    python plot_batch.py --config config.yaml
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

from utils import load_config, load_stations

# ---------------------------------------------------------------
# Style
# ---------------------------------------------------------------

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 9,
    "axes.linewidth": 0.6,
    "axes.labelsize": 10,
    "axes.titlesize": 11,
    "xtick.major.width": 0.5,
    "ytick.major.width": 0.5,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.4,
    "legend.fontsize": 7.5,
    "legend.framealpha": 0.9,
})

SITE_COLORS = {
    "bedrock": "#2166ac",
    "basin": "#b2182b",
    "classroom": "#4dac26",
    "remote": "#7570b3",
    "unknown": "#999999",
}

SITE_ORDER = ["bedrock", "basin"]

# Magnitude colormap
MAG_CMAP = plt.cm.plasma_r


def save_fig(fig, name: str, cfg: dict) -> None:
    fig_dir = Path(cfg["output_dir"]) / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    for fmt in cfg.get("figure_formats", ["png", "pdf"]):
        fig.savefig(fig_dir / f"{name}.{fmt}",
                    dpi=cfg.get("figure_dpi", 300),
                    bbox_inches="tight")
    print(f"  Saved {name}")


# ---------------------------------------------------------------
# Figure 1: SNR vs Distance (P and S panels)
# ---------------------------------------------------------------

def _binned_stats(distances, values, n_bins=8):
    """Compute binned median and IQR for trend lines."""
    mask = ~np.isnan(values)
    d, v = distances[mask], values[mask]
    if len(d) < 5:
        return None, None, None, None
    bin_edges = np.linspace(d.min(), d.max(), n_bins + 1)
    centers, medians, q25s, q75s = [], [], [], []
    for i in range(n_bins):
        in_bin = (d >= bin_edges[i]) & (d < bin_edges[i + 1])
        if i == n_bins - 1:
            in_bin = (d >= bin_edges[i]) & (d <= bin_edges[i + 1])
        if in_bin.sum() >= 3:
            centers.append((bin_edges[i] + bin_edges[i + 1]) / 2)
            medians.append(np.median(v[in_bin]))
            q25s.append(np.percentile(v[in_bin], 25))
            q75s.append(np.percentile(v[in_bin], 75))
    return np.array(centers), np.array(medians), np.array(q25s), np.array(q75s)


def fig_snr_vs_distance(df: pd.DataFrame, cfg: dict) -> None:
    """Side-by-side P and S SNR vs distance with per-station lines and group trends."""
    fig, (ax_p, ax_s) = plt.subplots(1, 2, figsize=(13, 5.5), sharey=True)

    det_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])

    for ax, phase, label in [(ax_p, "p", "P-phase"), (ax_s, "s", "S-phase")]:
        col = f"snr_db_{phase}_broadband"
        valid = df.dropna(subset=[col])

        # Per-station thin trend lines
        for site_type in SITE_ORDER:
            sub = valid[valid["site_type"] == site_type]
            color = SITE_COLORS[site_type]

            for sta in sub["station"].unique():
                sta_data = sub[sub["station"] == sta]
                centers, medians, _, _ = _binned_stats(
                    sta_data["distance_km"].values, sta_data[col].values, n_bins=6,
                )
                if centers is None or len(centers) < 2:
                    continue
                ax.plot(centers, medians, color=color, linewidth=0.8,
                        alpha=0.4, zorder=4)
                # Label at the right end
                ax.text(centers[-1] + 1.5, medians[-1], sta,
                        fontsize=5.5, color=color, va="center", alpha=0.7, zorder=6)

        # Bold group median + shaded IQR
        for site_type in SITE_ORDER:
            sub = valid[valid["site_type"] == site_type]
            if len(sub) < 5:
                continue
            centers, medians, q25, q75 = _binned_stats(
                sub["distance_km"].values, sub[col].values, n_bins=8,
            )
            if centers is None:
                continue
            color = SITE_COLORS[site_type]
            ax.plot(centers, medians, color=color, linewidth=2.5, zorder=8)
            ax.fill_between(centers, q25, q75, color=color, alpha=0.1, zorder=2)

        # Detection threshold
        ax.axhline(det_db, color="#e41a1c", linestyle=":", linewidth=0.7, alpha=0.5)
        ax.axhline(0, color="black", linestyle="-", linewidth=0.3, alpha=0.3)

        ax.set_xlabel("Epicentral Distance (km)")
        ax.set_title(label, fontweight="bold")

    ax_p.set_ylabel("SNR (dB)")

    # Legend
    handles = []
    for stype in SITE_ORDER:
        handles.append(Line2D([0], [0], color=SITE_COLORS[stype], linewidth=2.5,
                              label=f"{stype} (group median)"))
        handles.append(Line2D([0], [0], color=SITE_COLORS[stype], linewidth=0.8,
                              alpha=0.4, label=f"{stype} (individual stations)"))
    handles.append(Line2D([0], [0], linestyle=":", color="#e41a1c",
                          linewidth=0.7, label=f"Detection threshold ({det_db:.0f} dB)"))
    ax_p.legend(handles=handles, loc="upper right", fontsize=6.5)

    fig.suptitle("SNR vs. Epicentral Distance — Per-Station Trends (100 Regional Events)",
                 fontsize=12, fontweight="bold", y=1.02)

    save_fig(fig, "fig1_snr_vs_distance", cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 2: Station box plots by site type
# ---------------------------------------------------------------

def fig_station_boxplots(df: pd.DataFrame, cfg: dict) -> None:
    """Box plots of P and S SNR per station, grouped by site type."""
    # Order stations: group by site type, then sort by median combined SNR within group
    station_order = []
    for stype in SITE_ORDER:
        stype_stations = df[df["site_type"] == stype]["station"].unique()
        if len(stype_stations) == 0:
            continue
        medians = {}
        for sta in stype_stations:
            vals = df[df["station"] == sta]["snr_db_combined_broadband"].dropna()
            medians[sta] = vals.median() if len(vals) > 0 else -999
        for sta in sorted(medians, key=medians.get, reverse=True):
            station_order.append(sta)

    n = len(station_order)
    fig, (ax_p, ax_s) = plt.subplots(2, 1, figsize=(max(8, n * 0.7), 8), sharex=True)

    det_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])
    good_db = 20 * np.log10(cfg["thresholds"]["good_snr"])

    for ax, phase, title in [(ax_p, "p", "P-phase SNR"), (ax_s, "s", "S-phase SNR")]:
        col = f"snr_db_{phase}_broadband"
        box_data = []
        colors = []
        for sta in station_order:
            vals = df[df["station"] == sta][col].dropna().values
            box_data.append(vals if len(vals) > 0 else [np.nan])
            stype = df[df["station"] == sta]["site_type"].iloc[0]
            colors.append(SITE_COLORS.get(stype, "#999"))

        bp = ax.boxplot(box_data, positions=range(n), widths=0.6,
                        patch_artist=True, showfliers=True,
                        flierprops=dict(marker=".", markersize=2, alpha=0.3),
                        medianprops=dict(color="black", linewidth=1.2),
                        whiskerprops=dict(linewidth=0.6),
                        capprops=dict(linewidth=0.6))

        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.5)
            patch.set_edgecolor(color)

        ax.axhline(det_db, color="#e41a1c", linestyle=":", linewidth=0.7, alpha=0.6)
        ax.axhline(good_db, color="#2ca02c", linestyle=":", linewidth=0.7, alpha=0.6)
        ax.axhline(0, color="black", linestyle="-", linewidth=0.3, alpha=0.3)
        ax.set_ylabel("SNR (dB)")
        ax.set_title(title, fontweight="bold")

        # n= labels above each box
        for i, sta in enumerate(station_order):
            vals = df[df["station"] == sta][col].dropna()
            ax.text(i, ax.get_ylim()[1] * 0.95, f"n={len(vals)}", ha="center",
                    fontsize=5.5, color="#666")

    ax_s.set_xticks(range(n))
    ax_s.set_xticklabels(station_order, rotation=45, ha="right", fontsize=8, fontweight="bold")

    # Color station labels by site type
    for i, sta in enumerate(station_order):
        stype = df[df["station"] == sta]["site_type"].iloc[0]
        ax_s.get_xticklabels()[i].set_color(SITE_COLORS.get(stype, "#999"))

    # Add site type group brackets/separators
    prev_type = None
    for i, sta in enumerate(station_order):
        stype = df[df["station"] == sta]["site_type"].iloc[0]
        if prev_type is not None and stype != prev_type:
            for ax in (ax_p, ax_s):
                ax.axvline(i - 0.5, color="#cccccc", linewidth=0.5, linestyle="-")
        prev_type = stype

    # Legend
    handles = [Line2D([0], [0], color=SITE_COLORS[s], linewidth=6, alpha=0.5, label=s)
               for s in SITE_ORDER if s in df["site_type"].values]
    ax_p.legend(handles=handles, loc="upper right")

    fig.suptitle("Station SNR Distributions (100 Regional Events, broadband 1–45 Hz)",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()

    save_fig(fig, "fig2_station_boxplots", cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 3: Detection map with event cloud
# ---------------------------------------------------------------

def fig_detection_map(df: pd.DataFrame, events: list[dict], cfg: dict) -> None:
    """Map of station performance with event locations."""
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        has_cartopy = True
    except ImportError:
        has_cartopy = False
        print("  cartopy not installed — skipping map")
        return

    stations = load_stations(cfg["stations_file"])
    sta_dict = {s["station"]: s for s in stations}

    det_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])
    good_db = 20 * np.log10(cfg["thresholds"]["good_snr"])

    # Compute per-station mean combined SNR
    sta_stats = {}
    for sta in df["station"].unique():
        vals = df[df["station"] == sta]["snr_db_combined_broadband"].dropna()
        if len(vals) > 0 and sta in sta_dict:
            sta_stats[sta] = {
                "mean_snr": vals.mean(),
                "n_detect": (vals >= det_db).sum(),
                "n_total": len(vals),
                "lat": sta_dict[sta]["latitude"],
                "lon": sta_dict[sta]["longitude"],
                "site_type": df[df["station"] == sta]["site_type"].iloc[0],
            }

    # Extent
    all_lats = [s["lat"] for s in sta_stats.values()] + [e["latitude"] for e in events]
    all_lons = [s["lon"] for s in sta_stats.values()] + [e["longitude"] for e in events]
    pad = 0.5

    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
    ax.set_extent([min(all_lons) - pad, max(all_lons) + pad,
                   min(all_lats) - pad, max(all_lats) + pad])

    ax.add_feature(cfeature.BORDERS, linewidth=0.5, alpha=0.5)
    ax.add_feature(cfeature.STATES, linewidth=0.3, alpha=0.4)
    try:
        ax.add_feature(cfeature.LAND, facecolor="#f5f5f0", alpha=0.3)
    except Exception:
        pass

    # Gridlines
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, alpha=0.3,
                       color="gray", linestyle="--")
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 7}
    gl.ylabel_style = {"size": 7}

    # Event cloud
    ev_lats = [e["latitude"] for e in events]
    ev_lons = [e["longitude"] for e in events]
    ev_mags = [e["magnitude"] for e in events]
    ax.scatter(ev_lons, ev_lats, c=ev_mags, cmap=MAG_CMAP,
               s=15, alpha=0.4, edgecolors="none", zorder=3,
               transform=ccrs.PlateCarree())

    # Stations colored by performance
    for sta, info in sta_stats.items():
        mean_snr = info["mean_snr"]
        det_rate = info["n_detect"] / max(info["n_total"], 1)

        if mean_snr >= good_db:
            color = "#2ca02c"
        elif mean_snr >= det_db:
            color = "#ff7f0e"
        elif mean_snr >= 0:
            color = "#d62728"
        else:
            color = "#999999"

        size = 80 + det_rate * 120  # bigger = more detections

        ax.scatter(info["lon"], info["lat"],
                   c=color, s=size, edgecolors="black", linewidths=0.8,
                   marker="^", zorder=10, transform=ccrs.PlateCarree())

        # Label
        ax.text(info["lon"] + 0.06, info["lat"] + 0.06, sta,
                fontsize=6, fontweight="bold", color=color, zorder=11,
                transform=ccrs.PlateCarree())

        # Detection rate annotation
        pct = f"{det_rate*100:.0f}%"
        ax.text(info["lon"] + 0.06, info["lat"] - 0.06, pct,
                fontsize=5, color="#666", zorder=11,
                transform=ccrs.PlateCarree())

    # Network center
    ax.scatter(cfg["center_lon"], cfg["center_lat"],
               marker="+", c="black", s=50, linewidths=0.8, zorder=8,
               transform=ccrs.PlateCarree())

    # Legend
    handles = [
        Line2D([0], [0], marker="^", color="none", markerfacecolor="#2ca02c",
               markeredgecolor="black", markersize=10, label=f"Good (>{good_db:.0f} dB)"),
        Line2D([0], [0], marker="^", color="none", markerfacecolor="#ff7f0e",
               markeredgecolor="black", markersize=9, label=f"Marginal ({det_db:.0f}–{good_db:.0f} dB)"),
        Line2D([0], [0], marker="^", color="none", markerfacecolor="#d62728",
               markeredgecolor="black", markersize=9, label=f"Weak (0–{det_db:.0f} dB)"),
        Line2D([0], [0], marker="^", color="none", markerfacecolor="#999999",
               markeredgecolor="black", markersize=9, label="Below noise (< 0 dB)"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#aaa",
               markersize=5, alpha=0.4, label="Events (colored by mag)"),
    ]
    ax.legend(handles=handles, loc="lower left", fontsize=7, framealpha=0.9)

    ax.set_title("Network Detection Performance\n"
                 "100 Regional Events (M2.5+, < 300 km) — % = detection rate above threshold",
                 fontsize=11, fontweight="bold")

    save_fig(fig, "fig3_detection_map", cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Figure 4: Frequency-band comparison
# ---------------------------------------------------------------

def fig_frequency_bands(df: pd.DataFrame, cfg: dict) -> None:
    """Per-station SNR across frequency bands."""
    bands = list(cfg["snr"]["frequency_bands"].keys())
    band_labels = {
        "broadband": "Broadband\n(1–45 Hz)",
        "low": "Low\n(1–5 Hz)",
        "mid": "Mid\n(5–15 Hz)",
        "high": "High\n(15–40 Hz)",
    }

    # Order stations by site type then mean broadband SNR
    station_order = []
    for stype in SITE_ORDER:
        stype_stas = df[df["site_type"] == stype]["station"].unique()
        medians = {}
        for sta in stype_stas:
            vals = df[df["station"] == sta]["snr_db_combined_broadband"].dropna()
            medians[sta] = vals.median() if len(vals) > 0 else -999
        for sta in sorted(medians, key=medians.get, reverse=True):
            station_order.append(sta)

    n_sta = len(station_order)
    n_bands = len(bands)

    fig, axes = plt.subplots(1, n_bands, figsize=(3.5 * n_bands, max(5, n_sta * 0.4)),
                              sharey=True)

    det_db = 20 * np.log10(cfg["thresholds"]["detection_snr"])

    for ax, band in zip(axes, bands):
        col_p = f"snr_db_p_{band}"
        col_s = f"snr_db_s_{band}"

        y_pos = np.arange(n_sta)

        p_means = []
        s_means = []
        colors = []

        for sta in station_order:
            sub = df[df["station"] == sta]
            p_vals = sub[col_p].dropna()
            s_vals = sub[col_s].dropna()
            p_means.append(p_vals.mean() if len(p_vals) > 0 else np.nan)
            s_means.append(s_vals.mean() if len(s_vals) > 0 else np.nan)
            stype = sub["site_type"].iloc[0] if len(sub) > 0 else "unknown"
            colors.append(SITE_COLORS.get(stype, "#999"))

        p_means = np.array(p_means)
        s_means = np.array(s_means)

        bar_h = 0.35
        ax.barh(y_pos - bar_h / 2, p_means, bar_h, color=colors, alpha=0.5,
                edgecolor=[c for c in colors], linewidth=0.5, label="P-phase")
        ax.barh(y_pos + bar_h / 2, s_means, bar_h, color=colors, alpha=0.85,
                edgecolor=[c for c in colors], linewidth=0.5, label="S-phase")

        ax.axvline(0, color="black", linewidth=0.3)
        ax.axvline(det_db, color="#e41a1c", linestyle=":", linewidth=0.6, alpha=0.5)

        ax.set_xlabel("Mean SNR (dB)")
        ax.set_title(band_labels.get(band, band), fontweight="bold", fontsize=9)

        # Site type separators
        prev_type = None
        for i, sta in enumerate(station_order):
            stype = df[df["station"] == sta]["site_type"].iloc[0]
            if prev_type is not None and stype != prev_type:
                ax.axhline(i - 0.5, color="#cccccc", linewidth=0.5)
            prev_type = stype

    axes[0].set_yticks(range(n_sta))
    axes[0].set_yticklabels(station_order, fontsize=8, fontweight="bold")
    axes[0].invert_yaxis()

    # Color y-axis labels
    for i, sta in enumerate(station_order):
        stype = df[df["station"] == sta]["site_type"].iloc[0]
        axes[0].get_yticklabels()[i].set_color(SITE_COLORS.get(stype, "#999"))

    # Legend in first panel
    handles = [
        plt.Rectangle((0, 0), 1, 1, fc="gray", alpha=0.5, label="P-phase"),
        plt.Rectangle((0, 0), 1, 1, fc="gray", alpha=0.85, label="S-phase"),
    ]
    for stype in SITE_ORDER:
        if stype in df["site_type"].values:
            handles.append(plt.Rectangle((0, 0), 1, 1, fc=SITE_COLORS[stype],
                                          alpha=0.7, label=stype))
    axes[0].legend(handles=handles, loc="lower left", fontsize=6.5)

    fig.suptitle("Frequency-Dependent SNR by Station\n"
                 "Mean across 100 regional events (lighter = P, darker = S)",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()

    save_fig(fig, "fig4_frequency_bands", cfg)
    plt.close(fig)


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate batch SNR figures")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    results_csv = Path(cfg["output_dir"]) / "data" / "batch_snr_results.csv"
    events_file = Path(cfg["output_dir"]) / "data" / "batch_events.json"

    if not results_csv.exists():
        print(f"No results at {results_csv}. Run compute_batch.py first.")
        return

    df = pd.read_csv(results_csv)
    with open(events_file) as f:
        events = json.load(f)

    # Exclude stations
    exclude = cfg.get("exclude_stations", [])
    if exclude:
        df = df[~df["station"].isin(exclude)]

    # Remap site types from config
    site_types = cfg.get("site_types", {})
    sta_to_site = {}
    for stype, stas in site_types.items():
        for s in stas:
            sta_to_site[s] = stype
    df["site_type"] = df["station"].map(sta_to_site).fillna("unknown")

    # Drop stations with no data at all
    valid_stations = []
    for sta in df["station"].unique():
        if df[df["station"] == sta]["snr_db_combined_broadband"].dropna().shape[0] > 0:
            valid_stations.append(sta)
    df = df[df["station"].isin(valid_stations)]

    print(f"Loaded {len(df)} measurements, {len(valid_stations)} stations, {len(events)} events")
    print("\nGenerating figures...")

    fig_snr_vs_distance(df, cfg)
    fig_station_boxplots(df, cfg)
    fig_frequency_bands(df, cfg)

    print("\nDone.")


if __name__ == "__main__":
    main()
