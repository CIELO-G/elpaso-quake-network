"""1D layered velocity model for travel-time computation.

Convention
----------
Depth ``z`` is measured positive-down from a datum (sea level by default).
A station at elevation ``e_m`` metres above sea level sits at
``z = -e_m / 1000`` km in model coordinates. The topmost layer is treated
as extending upward to arbitrarily negative z, so elevated stations see
the same velocity as at the surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

Phase = Literal["P", "S"]


@dataclass(frozen=True)
class Layer:
    """A single layer in a 1D velocity model.

    Parameters
    ----------
    top_km : float
        Depth of the top of this layer, in km below datum. Ascending order
        is required across a ``LayeredModel``.
    vp : float
        P-wave velocity in this layer, km/s.
    vs : float
        S-wave velocity in this layer, km/s.
    """

    top_km: float
    vp: float
    vs: float

    def __post_init__(self) -> None:
        if self.vp <= 0 or self.vs <= 0:
            raise ValueError(f"velocities must be positive, got vp={self.vp}, vs={self.vs}")
        if self.vs >= self.vp:
            raise ValueError(f"vs must be < vp (got vp={self.vp}, vs={self.vs})")


class LayeredModel:
    """Flat-layered 1D velocity model.

    Layers are ordered ascending by ``top_km``. The deepest layer is treated
    as a half-space extending to infinity. The shallowest layer is treated
    as extending upward to arbitrary negative z (to support elevated
    stations).

    This is a flat-earth model — accurate to ~1% at sub-200 km distances,
    appropriate for the scale of a local/regional seismic network.
    """

    def __init__(self, layers: Sequence[Layer]) -> None:
        if not layers:
            raise ValueError("velocity model must have at least one layer")
        tops = [layer.top_km for layer in layers]
        for a, b in zip(tops, tops[1:]):
            if a >= b:
                raise ValueError(
                    f"layer tops must be strictly ascending, got {tops}"
                )
        self.layers: tuple[Layer, ...] = tuple(layers)

    def __repr__(self) -> str:
        return f"LayeredModel(n_layers={len(self.layers)})"

    def velocity(self, depth_km: float, phase: Phase) -> float:
        """Return the ``phase`` velocity (km/s) at the given depth.

        Depths above the top of the shallowest layer return that layer's
        velocity (top layer extends upward). Depths at or below the top of
        the deepest layer return that layer's velocity (bottom half-space).
        """
        if phase not in ("P", "S"):
            raise ValueError(f"phase must be 'P' or 'S', got {phase!r}")
        attr = "vp" if phase == "P" else "vs"
        # Find the deepest layer whose top is <= depth. If depth is shallower
        # than all layer tops (elevated station), use the shallowest layer.
        containing = self.layers[0]
        for layer in self.layers:
            if layer.top_km <= depth_km:
                containing = layer
            else:
                break
        return getattr(containing, attr)

    def max_velocity(self, z_lo: float, z_hi: float, phase: Phase) -> float:
        """Maximum ``phase`` velocity encountered along a ray from z_lo to z_hi.

        Used to set safe bounds on ray-parameter searches: for a direct
        ray to propagate through every layer along the path we need
        ``p * v < 1`` everywhere, hence ``p < 1 / v_max``.

        Velocity is sampled at the *midpoint* of each layer-segment along
        the path — identical to how :func:`lib.location.travel_times.
        _integrate_ray` samples — so a layer whose top sits exactly at an
        endpoint (and which the ray therefore does not enter) is not
        counted.
        """
        if z_lo == z_hi:
            return self.velocity(z_lo, phase)
        if z_lo > z_hi:
            z_lo, z_hi = z_hi, z_lo
        interior = sorted(
            layer.top_km for layer in self.layers if z_lo < layer.top_km < z_hi
        )
        waypoints = [z_lo] + interior + [z_hi]
        v_max = 0.0
        for z1, z2 in zip(waypoints, waypoints[1:]):
            mid = 0.5 * (z1 + z2)
            v = self.velocity(mid, phase)
            if v > v_max:
                v_max = v
        return v_max


# ---------------------------------------------------------------------------
# 1D model for El Paso / southern Rio Grande Rift.
#
# Originally derived from TexNet-style Permian Basin models (Frohlich et al.;
# Savvaidis et al.). Top three layers slowed ~5-8% from the published
# Permian values to account for the Hueco Bolson sedimentary basin under
# El Paso (Rio Grande Rift fill is slower than Permian sed cover); deeper
# crust + mantle are well-established regionally and unchanged.
#
# This is a calibration starting point — observed S-wave residuals on
# distant events (e.g. Carlsbad blasts ~130 km) suggested the original
# values were systematically too fast. Iterate further if residuals
# still show structure with distance or azimuth.
#
# Vp/Vs ~ 1.75 throughout (slightly above Poisson 1.732, typical of
# basement / crustal rocks with some saturation).
# ---------------------------------------------------------------------------
DEFAULT_WEST_TEXAS_MODEL = LayeredModel(
    [
        Layer(top_km=0.0, vp=3.30, vs=1.88),    # Hueco Bolson / basin fill (slowed from 3.60)
        Layer(top_km=1.0, vp=4.85, vs=2.80),    # Consolidated seds (slowed from 5.20)
        Layer(top_km=3.0, vp=5.50, vs=3.15),    # Upper crust / Precambrian basement (slowed from 5.80)
        Layer(top_km=10.0, vp=6.20, vs=3.58),   # Mid crust (unchanged)
        Layer(top_km=20.0, vp=6.60, vs=3.81),   # Lower crust (unchanged)
        Layer(top_km=35.0, vp=7.90, vs=4.56),   # Uppermost mantle / Moho (unchanged)
    ]
)
