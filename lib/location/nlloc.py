"""NonLinLoc-backed event locator. Drop-in for :class:`GridSearchLocator`.

Reads precomputed travel-time grids built once by
``scripts/nlloc_build_grids.py`` and shells out to the ``NLLoc`` binary
per relocation request. Each :meth:`NLLocLocator.locate` call writes a
temp NLLOC_OBS file + control file, runs NLLoc, parses the .hyp output
via ``obspy.io.nlloc``, and returns a :class:`LocationResult` matching
GridSearchLocator's shape so the dashboard relocate endpoint can swap
between the two with one line of code.

This wrapper depends on:
  * NLLoc binary on disk (env var ``NLLOC_BIN_DIR`` or sibling
    ``../NonLinLoc/src/bin/`` clone)
  * Travel-time grids at ``output/nlloc/grids/time/`` — run
    ``scripts/nlloc_build_grids.py`` once after install / station changes
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from lib.constants import PROJECT_ROOT
from lib.location.grid_search import LocationResult, Pick, Residual, Station


def _parse_sigma_z_from_statistics(hyp_path: Path) -> float:
    """Read the STATISTICS line and return σz = sqrt(CovZZ) in km.

    Falls back to 0.0 if the line / token isn't present. The covariance
    NLLoc reports is already in km² (projected coordinates), so a square
    root gives a depth standard deviation directly.
    """
    try:
        for line in hyp_path.read_text().splitlines():
            if line.startswith("STATISTICS"):
                tokens = line.split()
                if "ZZ" in tokens:
                    idx = tokens.index("ZZ")
                    return float(tokens[idx + 1]) ** 0.5
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _parse_quality_rms(hyp_path: Path) -> float:
    """Pull NLLoc's weighted RMS (seconds) from the QUALITY line.

    NLLoc computes a properly weighted RMS that suppresses contributions
    from low-weight picks; recomputing unweighted from arrivals would
    over-penalise outlier picks that NLLoc itself already de-weighted.
    """
    try:
        for line in hyp_path.read_text().splitlines():
            if line.startswith("QUALITY"):
                tokens = line.split()
                if "RMS" in tokens:
                    return float(tokens[tokens.index("RMS") + 1])
    except (OSError, ValueError, IndexError):
        pass
    return float("nan")


# ── Defaults (mirror scripts/nlloc_build_grids.py) ──────────────
_DEFAULT_BIN_DIR = PROJECT_ROOT.parent / "NonLinLoc" / "src" / "bin"
_DEFAULT_GRID_DIR = PROJECT_ROOT / "output" / "nlloc" / "grids" / "time"
_DEFAULT_GRID_BASENAME = "elpaso"

# Pick uncertainty (seconds, Gaussian σ). Matches GridSearchLocator
# defaults so the two locators see the same uncertainty model.
DEFAULT_SIGMA_P = 0.10
DEFAULT_SIGMA_S = 0.20


# ── Wrapper class ───────────────────────────────────────────────
class NLLocLocator:
    """NonLinLoc-backed locator. Mirrors :class:`GridSearchLocator`'s API."""

    def __init__(
        self,
        stations: Sequence[Station],
        *,
        bin_dir: Path | str | None = None,
        grid_dir: Path | str | None = None,
        grid_basename: str = _DEFAULT_GRID_BASENAME,
    ) -> None:
        if not stations:
            raise ValueError("at least one station required")

        env_bin = os.environ.get("NLLOC_BIN_DIR")
        self._bin_dir = Path(bin_dir or env_bin or _DEFAULT_BIN_DIR).resolve()
        self._nlloc_bin = self._bin_dir / "NLLoc"
        if not self._nlloc_bin.exists():
            raise FileNotFoundError(
                f"NLLoc binary not found at {self._nlloc_bin}. "
                "Build NonLinLoc and set NLLOC_BIN_DIR, or clone it to "
                f"{_DEFAULT_BIN_DIR.parent.parent}."
            )

        self._grid_dir = Path(grid_dir or _DEFAULT_GRID_DIR).resolve()
        self._grid_basename = grid_basename
        # Sanity-check at least one travel-time grid exists; surface the
        # build step rather than a cryptic NLLoc error on first call.
        sample_grid = (
            self._grid_dir / f"{grid_basename}.P.{stations[0].station_id.split('.')[-1]}.time.hdr"
        )
        if not sample_grid.exists():
            raise FileNotFoundError(
                f"No NLLoc travel-time grid for the first station at "
                f"{sample_grid}. Run scripts/nlloc_build_grids.py first."
            )

        self.stations: dict[str, Station] = {s.station_id: s for s in stations}

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    def locate(
        self,
        picks: Iterable[Pick],
        *,
        ref_epoch_unix: float | None = None,
        min_picks: int = 4,
        sigma_p: float = DEFAULT_SIGMA_P,
        sigma_s: float = DEFAULT_SIGMA_S,
        **_grid_kwargs,  # accepted for API parity, ignored (NLLoc has its own grid)
    ) -> LocationResult:
        """Locate one event from a pick set, returning a :class:`LocationResult`.

        Parameters
        ----------
        picks
            Iterable of :class:`Pick`. Picks with unknown ``station_id``
            are silently skipped. ``Pick.time_s`` is interpreted as Unix
            time when ``ref_epoch_unix`` is ``None``, or as seconds
            relative to that epoch otherwise.
        ref_epoch_unix
            Optional reference Unix timestamp. If provided, each pick's
            absolute UTC time is ``ref_epoch_unix + pick.time_s``. The
            dashboard relocate endpoint uses this to keep ``time_s``
            values numerically small.
        min_picks
            Reject the relocation if fewer than this many usable picks
            survive station filtering.
        sigma_p, sigma_s
            Default per-phase Gaussian pick error (seconds) used when a
            Pick doesn't carry its own ``sigma_s``.
        """
        # 1. Filter to known stations and substitute default sigmas
        usable: list[Pick] = []
        for pick in picks:
            if pick.station_id not in self.stations:
                continue
            sigma = pick.sigma_s
            if sigma is None:
                sigma = sigma_p if pick.phase == "P" else sigma_s
            usable.append(
                Pick(
                    station_id=pick.station_id,
                    phase=pick.phase,
                    time_s=pick.time_s,
                    sigma_s=sigma,
                )
            )
        if len(usable) < min_picks:
            raise ValueError(f"need at least {min_picks} usable picks, got {len(usable)}")

        with tempfile.TemporaryDirectory(prefix="nlloc_run_") as td_str:
            td = Path(td_str)
            obs_path = td / "event.obs"
            ctrl_path = td / "event.in"
            loc_dir = td / "loc"
            loc_dir.mkdir()

            self._write_obs(obs_path, usable, ref_epoch_unix)
            self._write_control(ctrl_path, obs_path, loc_dir)

            self._run_nlloc(ctrl_path, cwd=td)

            hyp_path = self._find_hyp_output(loc_dir)
            return self._parse_hyp(hyp_path, usable, ref_epoch_unix)

    # ------------------------------------------------------------------
    # Pick → NLLOC_OBS file
    # ------------------------------------------------------------------

    @staticmethod
    def _absolute_utc(pick: Pick, ref_epoch_unix: float | None) -> datetime:
        unix_s = (
            float(pick.time_s) if ref_epoch_unix is None else ref_epoch_unix + float(pick.time_s)
        )
        return datetime.fromtimestamp(unix_s, tz=timezone.utc)

    def _write_obs(self, path: Path, picks: list[Pick], ref_epoch_unix: float | None) -> None:
        """Write picks in NLLOC_OBS format (one phase per line).

        Field order (whitespace-separated, 15 fields):
          STA INST COMP P_ONSET PHASE F_MOTION
          YYYYMMDD HHMM SS.SSSS
          ERR_TYPE ERR CODA AMP PER PRIORWT

        We use ``?`` for unknown instrument / component / onset / first
        motion (NLLoc tolerates that), and ``-1`` for unknown coda /
        amplitude / period.
        """
        lines = []
        for pick in picks:
            utc = self._absolute_utc(pick, ref_epoch_unix)
            sta = pick.station_id.split(".")[-1]
            sec = utc.second + utc.microsecond / 1e6
            lines.append(
                f"{sta:<7} ? ? ? {pick.phase} ? "
                f"{utc:%Y%m%d} {utc:%H%M} {sec:8.4f}  "
                f"GAU {pick.sigma_s:.4e}  -1 -1 -1  1"
            )
        path.write_text("\n".join(lines) + "\n")

    # ------------------------------------------------------------------
    # Control file
    # ------------------------------------------------------------------

    def _write_control(self, path: Path, obs_path: Path, loc_out_dir: Path) -> None:
        """Render an NLLoc location control file pointing at our grids."""
        from lib.location.nlloc_config import (
            GRID_SPACING_KM,
            GRID_X_HALF_KM,
            GRID_Y_HALF_KM,
            GRID_Z_BOTTOM_KM,
            GRID_Z_TOP_KM,
            render_trans,
        )

        # Mirror the velocity-grid extents so the search and the precomputed
        # travel times share a coordinate system.
        nx = round(2 * GRID_X_HALF_KM / GRID_SPACING_KM) + 1
        ny = round(2 * GRID_Y_HALF_KM / GRID_SPACING_KM) + 1
        nz = round((GRID_Z_BOTTOM_KM - GRID_Z_TOP_KM) / GRID_SPACING_KM) + 1

        grid_in_base = (self._grid_dir / self._grid_basename).resolve()
        out_base = (loc_out_dir / "event").resolve()

        body = "\n".join(
            [
                "CONTROL 1 12345",
                render_trans(),
                "LOCSIG ElPasoSeismicNetwork",
                f"LOCFILES {obs_path}  NLLOC_OBS  {grid_in_base}  {out_base}",
                # NLLOC_V2 keeps the .hyp parseable by obspy.io.nlloc.core.
                "LOCHYPOUT SAVE_NLLOC_ALL",
                # Octree search — standard NLLoc probabilistic locator.
                #   init_num_cells_x/y/z  num_scatter_samples  init_num_cells_x/y/z
                #   ...  use_stations_density  stop_on_min_node_size
                "LOCSEARCH OCT 10 10 4 0.01 20000 5000 0 1",
                f"LOCGRID {nx} {ny} {nz}  "
                f"{-GRID_X_HALF_KM} {-GRID_Y_HALF_KM} {GRID_Z_TOP_KM}  "
                f"{GRID_SPACING_KM} {GRID_SPACING_KM} {GRID_SPACING_KM}  "
                "PROB_DENSITY  SAVE",
                # EDT_OT_WT: equal-differential-time origin-time weighted —
                # robust to outlier picks and bad timing. 1.68 = max picks/event,
                # 6 = min N to consider an event, rest are method-specific knobs.
                "LOCMETH EDT_OT_WT  9999.0  4  -1  -1  1.68  6  -1.0  1",
                # Pick error model: σ from the obs file (we set it per pick).
                "LOCGAU  0.2  0.0",
                "LOCGAU2 0.02 0.05 2.0",
                # Quality-to-error table (ignored when σ given per pick, but
                # NLLoc still wants the keyword).
                "LOCQUAL2ERR 0.1 0.5 1.0 2.0 99999.9",
                "LOCANGLES ANGLES_NO 5",
                "",
            ]
        )
        path.write_text(body)

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    def _run_nlloc(self, control_path: Path, cwd: Path) -> None:
        result = subprocess.run(
            [str(self._nlloc_bin), str(control_path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self._last_stdout = result.stdout
        self._last_stderr = result.stderr
        if result.returncode != 0:
            tail = "\n".join((result.stderr or result.stdout).splitlines()[-15:])
            raise RuntimeError(f"NLLoc failed (exit {result.returncode}):\n{tail}")

    @staticmethod
    def _find_hyp_output(loc_dir: Path) -> Path:
        """NLLoc writes both ``event.YYYYMMDD.HHMMSS.grid0.loc.hyp`` (per-event,
        with PHASE residual lines) and ``event.sum.grid0.loc.hyp`` (aggregated
        summary, no per-pick block). Prefer the per-event file so we get the
        arrivals/residuals; fall back to .sum if NLLoc only wrote that one.
        """
        candidates = sorted(loc_dir.glob("event.*.grid0.loc.hyp"))
        per_event = [c for c in candidates if ".sum." not in c.name]
        if per_event:
            return per_event[0]
        sum_files = [c for c in candidates if ".sum." in c.name]
        if sum_files:
            return sum_files[0]
        raise FileNotFoundError(
            f"No NLLoc .hyp output produced in {loc_dir} "
            f"(found: {[p.name for p in loc_dir.iterdir()]})"
        )

    # ------------------------------------------------------------------
    # .hyp → LocationResult
    # ------------------------------------------------------------------

    def _parse_hyp(
        self, hyp_path: Path, picks: list[Pick], ref_epoch_unix: float | None
    ) -> LocationResult:
        """Translate NLLoc's .hyp file into our LocationResult shape."""
        import numpy as np
        from obspy.io.nlloc.core import read_nlloc_hyp

        catalog = read_nlloc_hyp(str(hyp_path))
        if not catalog:
            tail = "\n".join((getattr(self, "_last_stdout", "") or "").splitlines()[-40:])
            files = sorted(p.name for p in hyp_path.parent.iterdir())
            raise RuntimeError(
                f"NLLoc produced no events in {hyp_path}\n"
                f"--- files in loc/: {files}\n"
                f"--- NLLoc stdout (last 40 lines) ---\n{tail}"
            )
        event = catalog[0]
        origin = event.preferred_origin() or event.origins[0]

        lat = float(origin.latitude)
        lon = float(origin.longitude)
        depth_km = float(origin.depth) / 1000.0  # NLLoc depth is metres

        # Origin time in the same frame the dashboard expects
        origin_unix = origin.time.timestamp
        if ref_epoch_unix is None:
            origin_time_s = origin_unix
        else:
            origin_time_s = origin_unix - ref_epoch_unix

        # Horizontal uncertainty ellipse. NLLoc's QML_OriginUncertainty line
        # gives us the 2D projection directly (min/max horizontal + azimuth);
        # obspy reads it into OriginUncertainty.{min,max}_horizontal_uncertainty
        # in metres. The full 3D ellipsoid is in confidence_ellipsoid but obspy
        # only populates that for some NLLoc output variants.
        h_major = h_minor = h_az = 0.0
        sigma_z = 0.0
        ou = origin.origin_uncertainty
        if ou:
            if ou.max_horizontal_uncertainty is not None:
                h_major = float(ou.max_horizontal_uncertainty) / 1000.0
            if ou.min_horizontal_uncertainty is not None:
                h_minor = float(ou.min_horizontal_uncertainty) / 1000.0
            if ou.azimuth_max_horizontal_uncertainty is not None:
                h_az = float(ou.azimuth_max_horizontal_uncertainty)
            # 3D ellipsoid (when present) gives a depth-axis proxy
            ce = ou.confidence_ellipsoid
            if ce and ce.semi_minor_axis_length is not None:
                sigma_z = float(ce.semi_minor_axis_length) / 1000.0
        # Fallback: depth std from the STATISTICS line in the .hyp text
        if sigma_z == 0.0:
            sigma_z = _parse_sigma_z_from_statistics(hyp_path)

        # Per-pick residuals from the event arrivals
        residuals: list[Residual] = []
        sqsum = 0.0
        n_used = 0
        for arr in event.preferred_origin().arrivals if event.preferred_origin() else []:
            res_s = float(arr.time_residual) if arr.time_residual is not None else 0.0
            weight = float(arr.time_weight) if arr.time_weight is not None else 1.0
            pick_ref = arr.pick_id.get_referred_object() if arr.pick_id else None
            if pick_ref is None:
                continue
            sta_code = pick_ref.waveform_id.station_code or "?"
            phase = (arr.phase or pick_ref.phase_hint or "?").upper()
            # Observed seconds in the same frame as input picks
            obs_unix = pick_ref.time.timestamp
            obs_s = obs_unix if ref_epoch_unix is None else obs_unix - ref_epoch_unix
            predicted_s = obs_s - res_s
            # We don't have NET prefix in NLLoc output; best-effort match
            sta_id_full = next(
                (sid for sid in self.stations if sid.split(".")[-1] == sta_code),
                f"?.{sta_code}",
            )
            residuals.append(
                Residual(
                    station_id=sta_id_full,
                    phase=phase,  # type: ignore[arg-type]
                    observed_s=obs_s,
                    predicted_s=predicted_s,
                    residual_s=res_s,
                    weight=weight,
                )
            )
            sqsum += res_s * res_s
            n_used += 1

        # NLLoc's weighted RMS from the QUALITY line is authoritative; ours
        # would be unweighted and over-penalise low-weight outliers.
        rms = _parse_quality_rms(hyp_path)

        return LocationResult(
            latitude=lat,
            longitude=lon,
            depth_km=depth_km,
            origin_time_s=origin_time_s,
            n_picks_used=n_used,
            rms_residual_s=rms,
            chi2_min=0.0,  # NLLoc reports likelihood, not chi²; leave 0 for now
            covariance_xyz_km2=np.full((3, 3), float("nan")),  # available from .scat
            horizontal_semi_axis_major_km=h_major,
            horizontal_semi_axis_minor_km=h_minor,
            horizontal_semi_axis_azimuth_deg=h_az,
            sigma_depth_km=sigma_z,
            residuals=tuple(residuals),
        )
