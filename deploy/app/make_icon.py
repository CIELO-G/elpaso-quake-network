#!/usr/bin/env python3
"""Generate the El Paso Monitor app icon (.iconset PNGs).

Draws a Big-Sur-style rounded square: dark slate background with an orange
seismogram trace (quiet background, P onset, larger S/Rg coda — the house
waveform look from the dashboard). Writes AppIcon.iconset/ next to this
script; deploy/build_app.sh turns that into AppIcon.icns via iconutil.

Run (any env with matplotlib + numpy + pillow):
    python deploy/app/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch
from PIL import Image

HERE = Path(__file__).resolve().parent
ICONSET = HERE / "AppIcon.iconset"
MASTER = HERE / "icon_1024.png"

BG = "#1e293b"       # dark slate
TRACE = "#f57c00"    # house orange
ACCENT = "#f5f0e6"   # cream


def synth_waveform(n: int = 2400, seed: int = 7) -> np.ndarray:
    """Quiet noise -> P onset -> bigger S/Rg coda, like a local blast."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    w = rng.normal(0, 0.02, n)
    p0, s0 = int(n * 0.30), int(n * 0.52)
    p_env = np.exp(-(t - p0) / (n * 0.05)) * (t >= p0)
    s_env = np.exp(-(t - s0) / (n * 0.16)) * (t >= s0)
    w += 0.35 * p_env * np.sin(2 * np.pi * (t - p0) / 28)
    w += 1.00 * s_env * np.sin(2 * np.pi * (t - s0) / 55)
    w += 0.25 * s_env * rng.normal(0, 0.5, n)
    return w / np.max(np.abs(w))


def draw_master(path: Path, px: int = 1024) -> None:
    fig = plt.figure(figsize=(px / 100, px / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    fig.patch.set_alpha(0)

    # Rounded-square background (macOS ~22% corner radius, small margin)
    m = 0.045
    ax.add_patch(FancyBboxPatch(
        (m, m), 1 - 2 * m, 1 - 2 * m,
        boxstyle="round,pad=0,rounding_size=0.21",
        facecolor=BG, edgecolor="none",
    ))

    # Seismogram trace
    w = synth_waveform()
    x = np.linspace(0.14, 0.86, w.size)
    y = 0.50 + w * 0.20
    ax.plot(x, y, color=TRACE, lw=9, solid_capstyle="round",
            solid_joinstyle="round", zorder=5)

    # Station marker (small cream triangle, lower left) + subtle baseline
    ax.plot([0.14, 0.86], [0.50, 0.50], color=ACCENT, lw=1.4, alpha=0.25, zorder=3)
    ax.plot([0.225], [0.245], marker="^", markersize=52, color=ACCENT,
            markeredgecolor="none", zorder=6)

    fig.savefig(path, transparent=True)
    plt.close(fig)


def main() -> None:
    draw_master(MASTER)
    ICONSET.mkdir(exist_ok=True)
    master = Image.open(MASTER)
    sizes = {
        "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
    }
    for name, size in sizes.items():
        master.resize((size, size), Image.LANCZOS).save(ICONSET / name)
    print(f"Wrote {ICONSET} ({len(sizes)} sizes)")


if __name__ == "__main__":
    main()
