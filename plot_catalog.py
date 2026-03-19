#!/usr/bin/env python
"""Quick plot of cataloged events and station locations on a basemap."""

import csv
import json
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.io.img_tiles as cimgt
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / "output" / "5-catalog" / "catalog.csv"
STATIONS_FILE = ROOT / "stations.json"


def load_stations():
    with open(STATIONS_FILE) as f:
        return json.load(f)


def load_catalog():
    events = []
    with open(CATALOG) as f:
        for row in csv.DictReader(f):
            events.append({
                "id": row["event_id"],
                "time": row["time"],
                "lat": float(row["latitude"]),
                "lon": float(row["longitude"]),
                "mag": float(row["magnitude"]),
                "depth": float(row["depth_km"]),
                "picks": int(row["num_picks"]),
            })
    return events


def main():
    stations = load_stations()
    events = load_catalog()

    # Map extent — pad around all points
    all_lons = [s["longitude"] for s in stations] + [e["lon"] for e in events]
    all_lats = [s["latitude"] for s in stations] + [e["lat"] for e in events]
    pad = 0.15
    extent = [min(all_lons) - pad, max(all_lons) + pad,
              min(all_lats) - pad, max(all_lats) + pad]

    # Set up cartopy with Stamen terrain tiles
    proj = ccrs.PlateCarree()
    fig, ax = plt.subplots(figsize=(13, 10), subplot_kw={"projection": proj})
    ax.set_extent(extent, crs=proj)

    # Basemap — try multiple terrain tile sources
    tile_loaded = False
    tile_sources = [
        ("GoogleTiles", lambda: cimgt.GoogleTiles(style="terrain")),
        ("Stamen", lambda: cimgt.Stamen("terrain-background")),
        ("OSM", lambda: cimgt.OSM()),
    ]
    for name, make_tiles in tile_sources:
        try:
            tiles = make_tiles()
            ax.add_image(tiles, 9)
            tile_loaded = True
            print(f"Using {name} terrain tiles")
            break
        except Exception as exc:
            print(f"{name} tiles failed: {exc}")
    if not tile_loaded:
        print("All tile sources failed, using cartopy features")
        ax.add_feature(cfeature.LAND, facecolor="#e8e4d8")
        ax.add_feature(cfeature.OCEAN, facecolor="#c6dfef")
        ax.add_feature(cfeature.BORDERS, linewidth=0.5)
        ax.add_feature(cfeature.STATES, linewidth=0.3, edgecolor="gray")

    # Always add borders/coastlines on top
    ax.add_feature(cfeature.BORDERS, linewidth=0.8, edgecolor="black")
    ax.add_feature(cfeature.STATES, linewidth=0.4, edgecolor="dimgray")

    # Gridlines
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="gray",
                       alpha=0.5, linestyle="--")
    gl.top_labels = False
    gl.right_labels = False

    # Plot stations
    sta_lons = [s["longitude"] for s in stations]
    sta_lats = [s["latitude"] for s in stations]
    sta_ids = [f'{s["network"]}.{s["station"]}' for s in stations]

    ax.scatter(sta_lons, sta_lats, marker="^", s=130, c="steelblue",
               edgecolors="black", linewidths=0.8, zorder=5,
               label=f"Stations ({len(stations)})", transform=proj)

    for lon, lat, sid in zip(sta_lons, sta_lats, sta_ids):
        ax.annotate(sid, (lon, lat), textcoords="offset points",
                    xytext=(6, 6), fontsize=7, fontweight="bold",
                    color="steelblue", transform=proj)

    # Plot events
    if events:
        ev_lons = [e["lon"] for e in events]
        ev_lats = [e["lat"] for e in events]
        ev_mags = [e["mag"] for e in events]
        ev_depths = [e["depth"] for e in events]

        sizes = [30 * (2 ** (m - 1)) for m in ev_mags]

        sc = ax.scatter(ev_lons, ev_lats, s=sizes, c=ev_depths,
                        cmap="YlOrRd", edgecolors="black", linewidths=0.8,
                        zorder=4, alpha=0.85, vmin=0, vmax=30, transform=proj)

        cbar = plt.colorbar(sc, ax=ax, shrink=0.5, pad=0.02)
        cbar.set_label("Depth (km)", fontsize=10)

        for e in events:
            label = f'M{e["mag"]:.1f}  {e["time"][:10]}'
            ax.annotate(label, (e["lon"], e["lat"]),
                        textcoords="offset points", xytext=(8, -10),
                        fontsize=7, color="darkred", fontweight="bold",
                        transform=proj)

        # Magnitude legend entries
        for mag_val in [1.5, 2.0, 2.5]:
            ax.scatter([], [], s=30 * (2 ** (mag_val - 1)), c="gray",
                       edgecolors="black", linewidths=0.8,
                       label=f"M{mag_val:.1f}")

    ax.set_title("El Paso Seismic Network — Detected Events", fontsize=14,
                 fontweight="bold")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)

    summary = f"{len(events)} events | {len(stations)} stations"
    if events:
        mags = [e["mag"] for e in events]
        summary += f" | M{min(mags):.1f}\u2013{max(mags):.1f}"
    ax.text(0.99, 0.01, summary, transform=ax.transAxes,
            ha="right", va="bottom", fontsize=9, color="white",
            bbox=dict(facecolor="black", alpha=0.5, boxstyle="round,pad=0.3"))

    out = ROOT / "output" / "catalog_map.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print(f"Saved to {out}")
    plt.show()


if __name__ == "__main__":
    main()
