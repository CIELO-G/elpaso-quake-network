"""Render NLLoc control-file fragments from our domain types.

NLLoc reads everything from a plain-text control file. Rather than maintain
a parallel hand-edited copy of the velocity model and station list, this
module renders the NLLoc-specific blocks from the existing ``LayeredModel``
and ``stations.json`` shapes so they stay in lock-step.

Three render helpers cover the three blocks that vary:
  - :func:`render_trans`  — coordinate transform (matches lib.projection)
  - :func:`render_layers` — per-layer LAYER lines from a ``LayeredModel``
  - :func:`render_vggrid` — velocity-grid dimensions (Vel2Grid + NLLoc)
  - :func:`render_gtsrce` — per-station GTSRCE lines for Grid2Time

The actual grid generation (running Vel2Grid + Grid2Time as subprocesses)
lives in ``scripts/nlloc_build_grids.py``.
"""

from __future__ import annotations

from typing import Iterable

from lib.constants import NETWORK_CENTER_LAT, NETWORK_CENTER_LON
from lib.location.velocity_model import LayeredModel


# Network-area grid extents (km from the projection origin). Widened from
# ±60×±90 to ±150×±150 so NLLoc can locate regional events (S-P up to ~30s,
# distance ~210 km) including quarry blasts at distant mines. Pairs with the
# GaMMA search region in 4-association/config.yaml.
# Spacing 0.5 km matches the location-precision floor set by pick timing
# (~10 ms PhaseNet → ~50 m equivalent) and the 1D velocity model.
# Per-station travel-time grid ~120 MB; total ~6.8 GB across 14 stations.
GRID_X_HALF_KM = 150.0
GRID_Y_HALF_KM = 150.0
GRID_Z_TOP_KM = -2.0    # negative = above sea level (elevated stations sit here)
GRID_Z_BOTTOM_KM = 38.0
GRID_SPACING_KM = 0.5


def gardner_density(vp_km_s: float) -> float:
    """Density (g/cm³) from P-wave velocity via Gardner et al. (1974).

    ρ = 1.74 × Vp^0.25. NLLoc requires a density per layer but travel times
    only depend on velocity, so this is a sensible default that keeps the
    layer block self-contained.
    """
    return 1.74 * (vp_km_s ** 0.25)


def render_trans(
    center_lat: float = NETWORK_CENTER_LAT,
    center_lon: float = NETWORK_CENTER_LON,
    rotation_deg: float = 0.0,
) -> str:
    """``TRANS SIMPLE`` line: local-cartesian centred on the network.

    Matches ``lib.projection.make_projection`` (stereographic) closely enough
    at the El Paso scale (~100 km) — both flatten the earth around the same
    origin with sub-km error.
    """
    return f"TRANS  SIMPLE  {center_lat}  {center_lon}  {rotation_deg}"


def render_layers(model: LayeredModel) -> str:
    """One LAYER line per layer (Vp / Vs from the model, Gardner density)."""
    lines = ["# LAYER  z_top   Vp    Vp_grad  Vs    Vs_grad  rho    rho_grad"]
    for layer in model.layers:
        rho = gardner_density(layer.vp)
        lines.append(
            f"LAYER    {layer.top_km:5.2f}  {layer.vp:.2f}  0.0      "
            f"{layer.vs:.2f}  0.0      {rho:.3f}  0.0"
        )
    return "\n".join(lines)


def _grid_node_count(min_km: float, max_km: float, spacing_km: float) -> int:
    return round((max_km - min_km) / spacing_km) + 1


def render_vggrid(
    phase: str = "P",
    *,
    spacing_km: float = GRID_SPACING_KM,
) -> str:
    """``VGTYPE`` + ``VGGRID`` for the velocity-grid generation step.

    Defaults give a 121 × 181 × 41 node grid (~898k nodes) at 1 km
    spacing — sub-second to build per phase and accurate to ML scale.
    """
    nx = _grid_node_count(-GRID_X_HALF_KM, GRID_X_HALF_KM, spacing_km)
    ny = _grid_node_count(-GRID_Y_HALF_KM, GRID_Y_HALF_KM, spacing_km)
    nz = _grid_node_count(GRID_Z_TOP_KM, GRID_Z_BOTTOM_KM, spacing_km)
    return (
        f"VGTYPE {phase}\n"
        f"VGGRID  {nx} {ny} {nz}   "
        f"{-GRID_X_HALF_KM} {-GRID_Y_HALF_KM} {GRID_Z_TOP_KM}   "
        f"{spacing_km} {spacing_km} {spacing_km}  SLOW_LEN"
    )


def render_gtsrce(stations: Iterable[dict]) -> str:
    """One GTSRCE line per station for the Grid2Time travel-time build.

    Format: ``GTSRCE label LATLON lat lon depth_km elev_above_datum_km``.
    Sea level is the depth datum; elevated stations get the ``elev`` field
    populated rather than a negative depth.
    """
    lines = [
        "# GTSRCE  label  LATLON   lat         lon          depth  elev_km",
    ]
    for s in stations:
        sta = s["station"]
        lat = float(s["latitude"])
        lon = float(s["longitude"])
        elev_km = float(s.get("elevation_m", 0.0) or 0.0) / 1000.0
        lines.append(
            f"GTSRCE  {sta:<7}  LATLON   "
            f"{lat:10.6f}  {lon:11.6f}  0.0    {elev_km:.3f}"
        )
    return "\n".join(lines)
