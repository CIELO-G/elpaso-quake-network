"""
El Paso Seismic Network - Base Map (v2 - Cleaned Up)
Station Quality Characterization Project
Author: Miranda
Date: March 2026

Creates a base map of seismometers across the greater El Paso region
for use in a research poster on station and data quality.

Dependencies:
    pip install cartopy matplotlib numpy geopandas osmnx shapely
    pip install matplotlib-scalebar adjustText pyproj
"""

import json
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import matplotlib.patheffects as pe
import numpy as np
from pathlib import Path

try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
except ImportError:
    print("Cartopy not installed. Run: pip install cartopy")
    exit(1)

try:
    import geopandas as gpd
except ImportError:
    gpd = None
    print("Warning: geopandas not installed. Geology/fault overlays disabled.")

try:
    from matplotlib_scalebar.scalebar import ScaleBar
except ImportError:
    ScaleBar = None
    print("Warning: matplotlib-scalebar not installed.")

try:
    from adjustText import adjust_text
    HAS_ADJUSTTEXT = True
except ImportError:
    HAS_ADJUSTTEXT = False
    print("Warning: adjustText not installed. Using manual offsets.")
    print("Run: pip install adjustText")


# =============================================================================
# 1. STATION DATA (loaded from stations.json)
# =============================================================================
_script_dir = Path(__file__).resolve().parent
_repo_root = _script_dir.parents[1]
_stations_file = _repo_root / "stations.json"
with open(_stations_file) as f:
    _station_list = json.load(f)

# Station ID: (lat, lon, elevation_m, network)
stations = {
    s["station"]: (s["latitude"], s["longitude"], s["elevation_m"], s["network"])
    for s in _station_list
}

rs_stations = {k: v for k, v in stations.items() if v[3] == "AM"}
bb_stations = {k: v for k, v in stations.items() if v[3] == "EP"}


# =============================================================================
# 2. MAP SETUP
# =============================================================================
all_lats = [v[0] for v in stations.values()]
all_lons = [v[1] for v in stations.values()]
lat_pad = 0.12
lon_pad = 0.08
extent = [
    min(all_lons) - lon_pad,
    max(all_lons) + lon_pad,
    min(all_lats) - lat_pad,
    max(all_lats) + lat_pad,
]

fig = plt.figure(figsize=(14, 12))
ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
ax.set_extent(extent, crs=ccrs.PlateCarree())



# =============================================================================
# 3. BASE LAYERS
# =============================================================================
ax.add_feature(cfeature.LAND, facecolor="#f5f0e6", zorder=0)
ax.add_feature(cfeature.OCEAN, facecolor="#d4e9f7", zorder=0)
ax.add_feature(cfeature.BORDERS, linestyle="--", linewidth=1.0, edgecolor="gray", zorder=2)
ax.add_feature(cfeature.STATES, linewidth=0.5, edgecolor="gray", zorder=2)


# =============================================================================
# 4. PLOT STATIONS
# =============================================================================
texts = []

# Raspberry Shake stations
for sid, (lat, lon, elev, net) in rs_stations.items():
    ax.plot(
        lon, lat,
        marker="v",
        color="#2166AC",
        markersize=16,
        markeredgecolor="black",
        markeredgewidth=0.8,
        transform=ccrs.PlateCarree(),
        zorder=5,
    )
    t = ax.text(
        lon, lat,
        f"  {sid}",
        fontsize=7,
        fontweight="bold",
        color="#2166AC",
        transform=ccrs.PlateCarree(),
        zorder=6,
        bbox=dict(
            boxstyle="round,pad=0.2",
            facecolor="white",
            alpha=0.85,
            edgecolor="none",
        ),
        path_effects=[pe.withStroke(linewidth=2, foreground="white")],
    )
    texts.append(t)

# Broadband station (KIDD)
for sid, (lat, lon, elev, net) in bb_stations.items():
    ax.plot(
        lon, lat,
        marker="v",
        color="#B2182B",
        markersize=18,
        markeredgecolor="black",
        markeredgewidth=0.8,
        transform=ccrs.PlateCarree(),
        zorder=5,
    )
    t = ax.text(
        lon, lat,
        f"  {sid} ",
        fontsize=7,
        fontweight="bold",
        color="#B2182B",
        transform=ccrs.PlateCarree(),
        zorder=6,
        bbox=dict(
            boxstyle="round,pad=0.2",
            facecolor="white",
            alpha=0.85,
            edgecolor="none",
        ),
        path_effects=[pe.withStroke(linewidth=2, foreground="white")],
    )
    texts.append(t)




# =============================================================================
# 6. FAULTS (USGS Quaternary Faults + highlighted EFMF)
# =============================================================================
if gpd is not None:
    from shapely.geometry import box as _box
    try:
        faults = gpd.read_file(str(_script_dir / "data" / "qfaults" / "SHP" / "Qfaults_US_Database.shp"))
        _bbox = _box(extent[0], extent[2], extent[1], extent[3])
        faults_clipped = faults[faults.geometry.intersects(_bbox)]
        faults_clipped.plot(
            ax=ax, color="red", linewidth=0.8, linestyle="--",
            alpha=0.6, transform=ccrs.PlateCarree(), zorder=4,
        )
        print(f"Loaded {len(faults_clipped)} fault segments.")
    except Exception as e:
        print(f"Could not load faults: {e}")



# =============================================================================
# 8. PLACEHOLDER: GEOLOGIC BASEMAP
# =============================================================================
if gpd is not None:
    from shapely.geometry import box

    try:
        geo = gpd.read_file(
            str(_script_dir / "data" / "geology" / "Geology_CONUS.shp"),
            bbox=(extent[0], extent[2], extent[1], extent[3]),
        )

        # Clip to map extent
        bbox_poly = box(extent[0], extent[2], extent[1], extent[3])
        geo_clipped = geo[geo.geometry.intersects(bbox_poly)]

        # Geologic color scheme (keyed on CMMI_Class)
        geo_colors = {
            "Other_Unconsolidated":            "#FFFFB3",  # pale yellow (alluvium)
            "Sedimentary_Chemical_Carbonate":  "#80B1D3",  # blue-gray (limestone)
            "Sedimentary_Siliciclastic":       "#B3DE69",  # light green (sandstone/shale)
            "Igneous_Extrusive":               "#FB8072",  # salmon red (volcanic)
            "Igneous_Intrusive_Felsic":        "#BC80BD",  # purple (granite)
        }

        geo_clipped = geo_clipped.copy()
        geo_clipped["color"] = geo_clipped["CMMI_Class"].map(geo_colors).fillna("#E0E0E0")

        geo_clipped.plot(
            ax=ax,
            color=geo_clipped["color"],
            alpha=0.5,
            transform=ccrs.PlateCarree(),
            zorder=1,
        )

        # Geology legend
        geo_patches = []
        # Pretty labels for the legend
        geo_labels = {
            "Other_Unconsolidated":            "Unconsolidated (alluvium)",
            "Sedimentary_Chemical_Carbonate":  "Carbonate (limestone)",
            "Sedimentary_Siliciclastic":       "Siliciclastic (sandstone/shale)",
            "Igneous_Extrusive":               "Igneous, extrusive (volcanic)",
            "Igneous_Intrusive_Felsic":        "Igneous, intrusive (granite)",
        }
        for key, color in geo_colors.items():
            if key in geo_clipped["CMMI_Class"].values:
                geo_patches.append(mpatches.Patch(facecolor=color, alpha=0.5,
                                                   edgecolor="gray", linewidth=0.5,
                                                   label=geo_labels.get(key, key)))
        geo_legend = ax.legend(
            handles=geo_patches,
            loc="upper left",
            fontsize=5,
            title="Geology",
            title_fontsize=6,
            framealpha=0.9,
            edgecolor="gray",
            fancybox=True,
            borderpad=0.8,
        )
        ax.add_artist(geo_legend)

        print(f"Loaded {len(geo_clipped)} geologic units.")
    except Exception as e:
        print(f"Could not load geology: {e}")

# =============================================================================
# 9b. CATALOG EVENT OVERLAY
# =============================================================================
import csv

_catalog_file = _repo_root / "output" / "5-catalog" / "catalog.csv"
if _catalog_file.exists():
    with open(_catalog_file) as f:
        reader = csv.DictReader(f)
        events = list(reader)

    ev_lats = [float(e["latitude"]) for e in events]
    ev_lons = [float(e["longitude"]) for e in events]

    ax.scatter(
        ev_lons, ev_lats,
        s=60,
        color="red",
        edgecolors="black",
        linewidths=0.5,
        alpha=0.8,
        transform=ccrs.PlateCarree(),
        zorder=5,
    )

    print(f"Plotted {len(events)} catalog events.")
else:
    print("No catalog file found — skipping event overlay.")

# =============================================================================
# 10. AUTO-ADJUST LABELS
# =============================================================================
if HAS_ADJUSTTEXT:
    x_points = [v[1] for v in stations.values()]
    y_points = [v[0] for v in stations.values()]

    adjust_text(
        texts,
        x=x_points,
        y=y_points,
        ax=ax,
        force_text=(0.8, 0.8),
        force_points=(1.2, 1.2),
        expand_text=(1.3, 1.3),
        arrowprops=dict(
            arrowstyle="-",
            color="gray",
            linewidth=0.5,
            alpha=0.6,
        ),
        avoid_self=True,
        only_move={"text": "xy"},
        max_move=None,
    )


# =============================================================================
# 11. MAP FURNITURE: Grid, Scale Bar, North Arrow, Legend
# =============================================================================
# Coordinate grid
gl = ax.gridlines(
    draw_labels=True,
    linewidth=0.4,
    color="gray",
    alpha=0.4,
    linestyle="--",
)
gl.top_labels = False
gl.right_labels = False
gl.xlabel_style = {"size": 8, "color": "#555555"}
gl.ylabel_style = {"size": 8, "color": "#555555"}

# Scale bar (manual, 20 km)
# At ~31.8°N, 1° lon ≈ 94.5 km → 20 km ≈ 0.2116°
scale_x_start = extent[1] - 0.42
scale_y = extent[2] + 0.06
km_20_in_deg = 20 / 94.5

ax.plot(
    [scale_x_start, scale_x_start + km_20_in_deg],
    [scale_y, scale_y],
    color="black", linewidth=3,
    transform=ccrs.PlateCarree(), zorder=7,
)
for x in [scale_x_start, scale_x_start + km_20_in_deg]:
    ax.plot(
        [x, x], [scale_y - 0.01, scale_y + 0.01],
        color="black", linewidth=2,
        transform=ccrs.PlateCarree(), zorder=7,
    )
ax.text(
    scale_x_start + km_20_in_deg / 2, scale_y + 0.025,
    "20 km", fontsize=8, ha="center", fontweight="bold",
    transform=ccrs.PlateCarree(), zorder=7,
)

# North arrow
arrow_x = extent[1] - 0.06
arrow_y = extent[3] - 0.08
ax.annotate(
    "N",
    xy=(arrow_x, arrow_y),
    xytext=(arrow_x, arrow_y - 0.1),
    fontsize=14, fontweight="bold", ha="center", va="center",
    arrowprops=dict(arrowstyle="->", lw=2.5, color="black"),
    transform=ccrs.PlateCarree(), zorder=7,
)
# Legend
rs_marker = mlines.Line2D(
    [], [], color="#2166AC", marker="v", linestyle="None",
    markersize=10, markeredgecolor="black", markeredgewidth=0.8,
    label="Raspberry Shake (AM)",
)
bb_marker = mlines.Line2D(
    [], [], color="#B2182B", marker="v", linestyle="None",
    markersize=10, markeredgecolor="black", markeredgewidth=0.8,
    label="KIDD (EP)",
)
fault_line = mlines.Line2D(
    [], [], color="red", linestyle="--", linewidth=0.8,
    alpha=0.6, label="Quaternary Fault",
)
event_marker = mlines.Line2D(
    [], [], color="red", marker="o", linestyle="None",
    markersize=6, markeredgecolor="black", markeredgewidth=0.5,
    label="Detected Event",
)

ax.legend(
    handles=[rs_marker, bb_marker, fault_line, event_marker],
    loc="lower left",
    fontsize=7,
    framealpha=0.95,
    edgecolor="gray",
    fancybox=True,
    borderpad=1.0,
)


# =============================================================================
# 12. EXPORT
# =============================================================================
plt.tight_layout(rect=[0, 0, 1, 0.95])

_out_png = _script_dir / "el_paso_seismic_network.png"
_out_pdf = _script_dir / "el_paso_seismic_network.pdf"
fig.savefig(_out_png, dpi=300, bbox_inches="tight", facecolor="white")
fig.savefig(_out_pdf, dpi=300, bbox_inches="tight", facecolor="white")

print(f"Map saved: {_out_png.name} / {_out_pdf.name}")
plt.show()
