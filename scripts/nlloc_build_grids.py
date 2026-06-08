#!/usr/bin/env python3
"""Build the NonLinLoc velocity grid + per-station travel-time grids.

Runs as a one-time setup step after installing NonLinLoc. Re-run whenever
``stations.json`` or the velocity model changes; the dashboard relocate
endpoint reads the cached grids on every request.

Outputs land in ``output/nlloc/grids/`` (gitignored). Total size: ~50 MB
for the El Paso network at 1 km grid spacing.

Usage
-----
    # Use the default sibling NonLinLoc clone at ../NonLinLoc/src/bin/
    python scripts/nlloc_build_grids.py

    # Or point at a different install
    NLLOC_BIN_DIR=/opt/nlloc/bin python scripts/nlloc_build_grids.py

    # Wipe and regenerate everything from scratch
    python scripts/nlloc_build_grids.py --force
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.config import load_stations  # noqa: E402
from lib.location.nlloc_config import (  # noqa: E402
    render_gtsrce,
    render_layers,
    render_trans,
    render_vggrid,
)
from lib.location.velocity_model import DEFAULT_WEST_TEXAS_MODEL  # noqa: E402

GRID_DIR = ROOT / "output" / "nlloc" / "grids"
TIME_DIR = GRID_DIR / "time"
GRID_BASENAME = "elpaso"
DEFAULT_NLLOC_BIN_DIR = ROOT.parent / "NonLinLoc" / "src" / "bin"

# Quiet NLLoc — only show our own log lines on success
NLLOC_CONTROL_MESSAGE_FLAG = 1


def _resolve_bin_dir() -> Path:
    """Locate the NonLinLoc binary directory.

    Priority: ``NLLOC_BIN_DIR`` env var → default sibling-clone path. Errors
    early with install instructions rather than letting subprocess discovery
    fail mid-run.
    """
    env_dir = os.environ.get("NLLOC_BIN_DIR")
    candidate = Path(env_dir) if env_dir else DEFAULT_NLLOC_BIN_DIR
    candidate = candidate.resolve()
    for required in ("Vel2Grid", "Grid2Time"):
        if not (candidate / required).exists():
            sys.stderr.write(
                f"ERROR: {required} not found in {candidate}\n"
                "Build NonLinLoc and set NLLOC_BIN_DIR, or clone it to "
                f"{DEFAULT_NLLOC_BIN_DIR.parent.parent}.\n"
                "  git clone https://github.com/alomax/NonLinLoc.git "
                f"{DEFAULT_NLLOC_BIN_DIR.parent.parent}\n"
                f"  cd {DEFAULT_NLLOC_BIN_DIR.parent} && cmake . && make -j\n"
            )
            sys.exit(2)
    return candidate


def _write_vel2grid_control(path: Path, phase: str) -> None:
    """Render a complete Vel2Grid control file for a single phase."""
    grid_out = (GRID_DIR / GRID_BASENAME).resolve()
    body = "\n".join(
        [
            f"CONTROL {NLLOC_CONTROL_MESSAGE_FLAG} 12345",
            render_trans(),
            f"VGOUT  {grid_out}",
            render_vggrid(phase),
            render_layers(DEFAULT_WEST_TEXAS_MODEL),
            "",
        ]
    )
    path.write_text(body)


def _write_grid2time_control(path: Path, phase: str, stations: list[dict]) -> None:
    """Render a Grid2Time control file (model grid → per-station TT grids)."""
    grid_in = (GRID_DIR / GRID_BASENAME).resolve()
    time_out = (TIME_DIR / GRID_BASENAME).resolve()
    body = "\n".join(
        [
            f"CONTROL {NLLOC_CONTROL_MESSAGE_FLAG} 12345",
            render_trans(),
            # GTFILES: velocity_grid_in, time_grid_out, phase, iSwapBytesOnInput
            f"GTFILES  {grid_in}  {time_out}  {phase}  0",
            # GRID3D = compute TT through the 3D velocity grid (works for 1D too).
            # ANGLES_YES = also write takeoff angle grids (we won't use them yet
            # but they're cheap and unlock first-motion focal mechanisms later).
            "GTMODE  GRID3D  ANGLES_YES",
            # Travel-time method: Podvin-Lecomte finite-difference. Eikonal solver
            # appropriate for crustal models; ~10⁻³ s convergence is fine for ML scale.
            "GT_PLFD  1.0e-3  0",
            render_gtsrce(stations),
            "",
        ]
    )
    path.write_text(body)


def _run(binary: Path, control_file: Path, cwd: Path) -> None:
    """Run an NLLoc-family binary and surface failures clearly."""
    result = subprocess.run(
        [str(binary), str(control_file)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(
            f"\n{binary.name} failed (exit {result.returncode})\n"
            f"--- stdout (last 30 lines) ---\n"
            f"{chr(10).join(result.stdout.splitlines()[-30:])}\n"
            f"--- stderr (last 30 lines) ---\n"
            f"{chr(10).join(result.stderr.splitlines()[-30:])}\n"
        )
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe output/nlloc/grids/ before rebuilding",
    )
    args = parser.parse_args()

    bin_dir = _resolve_bin_dir()
    print(f"NonLinLoc binaries: {bin_dir}")

    if args.force and GRID_DIR.exists():
        print(f"Wiping {GRID_DIR.relative_to(ROOT)} (--force)")
        shutil.rmtree(GRID_DIR)
    GRID_DIR.mkdir(parents=True, exist_ok=True)
    TIME_DIR.mkdir(parents=True, exist_ok=True)

    stations = load_stations(str(ROOT / "stations.json"))
    print(f"Stations: {len(stations)}  ({', '.join(s['station'] for s in stations)})")

    with tempfile.TemporaryDirectory(prefix="nlloc_build_") as td_str:
        td = Path(td_str)

        for phase in ("P", "S"):
            print(f"\n=== {phase} phase ===")

            t0 = time.monotonic()
            v2g_control = td / f"vel2grid_{phase}.in"
            _write_vel2grid_control(v2g_control, phase)
            _run(bin_dir / "Vel2Grid", v2g_control, cwd=ROOT)
            print(f"  Vel2Grid {phase}: {time.monotonic() - t0:.1f}s")

            t0 = time.monotonic()
            g2t_control = td / f"grid2time_{phase}.in"
            _write_grid2time_control(g2t_control, phase, stations)
            _run(bin_dir / "Grid2Time", g2t_control, cwd=ROOT)
            print(f"  Grid2Time {phase}: {time.monotonic() - t0:.1f}s")

    # Quick inventory of what got built
    n_model_files = len(list(GRID_DIR.glob(f"{GRID_BASENAME}.*.mod.*")))
    n_time_files = len(list(TIME_DIR.glob(f"{GRID_BASENAME}.*.time.*")))
    total_mb = sum(f.stat().st_size for f in GRID_DIR.rglob("*") if f.is_file()) / (1024 * 1024)
    print(
        f"\nDone. model files={n_model_files}  time files={n_time_files}  total={total_mb:.1f} MB"
    )


if __name__ == "__main__":
    main()
