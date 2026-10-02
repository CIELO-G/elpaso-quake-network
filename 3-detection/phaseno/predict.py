#!/usr/bin/env python3
"""PhaseNO production picker for the El Paso network (step-3 alternative).

Wraps the UNMODIFIED upstream model (phaseno_model.py, verbatim from the
notebook; weights in models/) with the plumbing the notebook lacks for
unattended use:

  * whole-day directory input in hourly chunks with 40-s context padding
    (a multiple of the 20-s window step, so every chunk shares one
    window grid and picks cannot shift or double at chunk seams; the
    peak-spacing rule is re-applied across seams)
  * channel selection per station model exactly as detect.py does
    (RS4D: EHZ only, vertical duplicated; broadband HH?: 3-comp; other
    Shakes: EH 3-comp) — accelerometer EN? channels are never used
  * gap handling: masked samples (short files) and runs of exact zeros
    >= 1 s (ingest's zero-fill) are treated as gaps; picks within 1 s of
    a gap edge are dropped; stations with < 50 % valid samples in a
    chunk are removed from the graph for that chunk. NOTE: 2-processed
    data has gaps *interpolated* by process.py and those cannot be
    recovered here — prefer 1-raw input (--input-units counts) when
    gap-robustness matters.
  * station coordinates normalised around ONE fixed centre (mean of all
    stations.json entries), so model inputs do not depend on which
    stations happen to be online in a chunk
  * fails loudly: missing day dir, no station data, or a crashed chunk
    -> no CSV and non-zero exit (an empty CSV means "ran, found nothing")
  * Apple MPS / CUDA / CPU device selection
  * writes the pipeline's picks CSV schema (same columns as detect.py),
    model="PhaseNO:v1.0.1", amplitudes in m/s on the vertical
  * sample alignment: traces are placed on the chunk grid by their first
    sample >= chunk start (notebook convention); station clock offsets
    of up to +-5 ms are therefore absorbed into pick times (same order as
    the pipeline's PhaseNet path)

Numerics vs the notebook (verified by an independent equivalence run on
a real hour): identical filter (obspy Trace.filter('highpass') = causal
4-pole Butterworth, sosfilt), identical z-score/scale, coordinate and
edge construction, forward, sigmoid, overlap averaging and peak picker
(the notebook's _detect_peaks is vendored). Interior probabilities agree
to ~2e-6 (float32 round-trip) with identical picks. The only deliberate
deviation is at a chunk's final 30 s, where the notebook zero-pads a
partial window and this code runs a full window ending at the chunk end
— the padding discards that region except for the last chunk of a day.

Run (project root, `phaseno` conda env):

    python 3-detection/phaseno/predict.py --day-dir output/1-raw/2026/159 \
        --stations stations.json --input-units counts \
        --out output/3-picks-phaseno/2026/159/2026.159.picks.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

log = logging.getLogger("phaseno.predict")

CSV_COLUMNS = [
    "network", "station", "location", "channel", "phase", "time",
    "probability", "model", "amplitude", "amplitude_channel",
]
MODEL_TAG = "PhaseNO:v1.0.1"

# ── upstream constants: the trained model's input contract ───────────
FS = 100                  # Hz
IN_SAMPLES = 3000         # 30-s window
OVERLAP = 1000            # 10-s overlap
STEP = IN_SAMPLES - OVERLAP
EPS = 1e-6
INPUT_SCALE = 10.0        # z-scored data / 10
BOX_HALF_DEG = 1.0        # coords normalised in a +-1 deg box
NORM_KM_PER_UNIT = 222.0  # upstream: dis_range/222


class DayError(RuntimeError):
    """Raised when a day cannot be processed (no CSV must be written)."""


@dataclass
class Params:
    checkpoint: Path = HERE / "models" / "epoch=19-step=1140000.ckpt"
    device: str = "auto"
    p_threshold: float = 0.4
    s_threshold: float = 0.4
    min_peak_distance: int = 100       # samples (upstream mpd = 1 s)
    highpass_hz: float = 1.0           # upstream default
    amp_highpass_hz: float = 3.0       # pipeline convention for amplitudes
    dis_range_km: float = 150.0        # >= network aperture -> full graph
    chunk_hours: float = 1.0
    pad_s: float = 40.0                # must be a multiple of STEP/FS
    gap_guard_s: float = 1.0
    zero_run_s: float = 1.0            # exact-zero run treated as a gap
    min_valid_fraction: float = 0.5    # station kept in a chunk's graph
    amp_window_p_s: float = 8.0        # upstream extract_amplitude
    amp_window_s_s: float = 4.0
    input_units: str = "velocity"      # or "counts"


@dataclass
class StationData:
    network: str
    station: str
    location: str
    chan_prefix: str
    lon: float
    lat: float
    sensitivity: float
    data: np.ndarray = field(repr=False)   # [3, nt] float64, E,N,Z
    valid: np.ndarray = field(repr=False)  # [nt] bool
    ncomp: int = 3


# ── stations ─────────────────────────────────────────────────────────
def station_sensitivity(s: dict) -> float:
    """Counts -> m/s divisor, mirroring detect.build_phasenet_stations."""
    inst = s.get("instrument", {})
    if "geophone" in inst:
        return float(inst["geophone"]["sensitivity"])
    if inst:
        return float(next(iter(inst.values()))["sensitivity"])
    return 1.0


def station_channels(s: dict) -> tuple[str, list[str]]:
    """(channel prefix, components) exactly as detect.build_phasenet_stations."""
    if s.get("model") == "RS4D":
        return "EH", ["Z"]
    if s.get("channels") == "HH?":
        return "HH", ["E", "N", "Z"]
    return "EH", ["E", "N", "Z"]


def network_center(stations: list[dict]) -> tuple[float, float]:
    """One fixed normalisation centre for every chunk and every day."""
    return (float(np.mean([s["longitude"] for s in stations])),
            float(np.mean([s["latitude"] for s in stations])))


def normalize_coords(lons, lats, center):
    """Upstream mapping: (coord - (center - 1 deg)) / 2  -> ~[0, 1]."""
    lons, lats = np.asarray(lons, float), np.asarray(lats, float)
    x = (lons - (center[0] - BOX_HALF_DEG)) / 2.0
    y = (lats - (center[1] - BOX_HALF_DEG)) / 2.0
    if np.any((x < 0) | (x > 1) | (y < 0) | (y > 1)):
        raise ValueError("stations exceed the +-1 deg normalisation box")
    return np.stack([x, y], axis=1).astype(np.float32)


def build_edges(coords: np.ndarray, dis_range_km: float) -> np.ndarray:
    """Upstream edge list [6, E]: i, j, xi, yi, xj, yj for all ordered
    pairs (self-loops included) within dis_range."""
    n = len(coords)
    thr = dis_range_km / NORM_KM_PER_UNIT
    rows = [[], [], [], [], [], []]
    for i in range(n):
        for j in range(n):
            if float(np.hypot(*(coords[i] - coords[j]))) <= thr:
                for k, v in enumerate([i, j, coords[i, 0], coords[i, 1],
                                       coords[j, 0], coords[j, 1]]):
                    rows[k].append(float(v))
    return np.asarray(rows, dtype=np.float32)


# ── waveform loading (gap-aware) ─────────────────────────────────────
def zero_runs(x: np.ndarray, min_len: int) -> np.ndarray:
    """True where x sits inside a run of exact zeros >= min_len samples."""
    z = (x == 0)
    if not z.any():
        return np.zeros(len(x), bool)
    d = np.diff(np.concatenate(([0], z.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    out = np.zeros(len(x), bool)
    for a, b in zip(starts, ends):
        if b - a >= min_len:
            out[a:b] = True
    return out


def load_day(day_dir: Path, stations: list[dict], t0, t1, params: Params):
    """Load [t0, t1) for every station with usable data, on a common
    100-Hz grid. Gaps -> valid=False (data held at 0)."""
    from obspy import Stream, read

    nt = int(round((t1 - t0) * FS))
    zr = int(params.zero_run_s * FS)
    out: list[StationData] = []
    seen = set()
    for s in stations:
        net, code = s["network"], s["station"]
        prefix, want = station_channels(s)
        files = sorted(day_dir.glob(f"{net}.{code}.*.mseed"))
        streams: dict[str, Stream] = {c: Stream() for c in "ENZ"}
        for fp in files:
            try:
                st = read(str(fp))
            except Exception as exc:
                log.warning("%s: unreadable (%s)", fp.name, exc)
                continue
            for tr in st:
                cha = tr.stats.channel
                if len(cha) < 3 or cha[:2] != prefix or cha[-1] not in "ENZ":
                    continue
                if cha[-1] not in want:
                    continue
                if abs(tr.stats.sampling_rate - FS) > 0.5:
                    tr.resample(FS)
                streams[cha[-1]] += tr
        if len(streams["Z"]) == 0:
            if files:
                log.warning("%s.%s: no %sZ channel in %d file(s), skipped",
                            net, code, prefix, len(files))
            continue
        seen.add(code)
        comps, masks = {}, {}
        for c, st in streams.items():
            if len(st) == 0:
                continue
            st.merge(method=1, fill_value=None)
            # nearest_sample=False: the first kept sample is the first one
            # >= t0. Station clocks put sample grids up to +-5 ms off the
            # window start; nearest-sample trimming would round a start
            # 0.9 samples late to -1 and pad a phantom leading sample,
            # shifting that station 10 ms relative to the others (and to
            # the notebook, which aligns traces by sample index).
            st.trim(t0, t1, pad=True, fill_value=None, nearest_sample=False)
            d = st[0].data
            mask = np.ma.getmaskarray(d) if np.ma.isMaskedArray(d) \
                else np.zeros(len(d), bool)
            arr = np.ma.filled(d, 0.0).astype(np.float64)[:nt]
            mask = mask[:nt]
            if len(arr) < nt:
                padn = nt - len(arr)
                arr = np.pad(arr, (0, padn))
                mask = np.pad(mask, (0, padn), constant_values=True)
            mask |= zero_runs(arr, zr)
            comps[c], masks[c] = arr, mask
        order = [comps.get("E", comps["Z"]), comps.get("N", comps["Z"]), comps["Z"]]
        valid = ~(masks.get("E", masks["Z"]) | masks.get("N", masks["Z"]) | masks["Z"])
        data = np.stack(order)
        if params.input_units == "counts":
            data = data / station_sensitivity(s)
        data[:, ~valid] = 0.0
        out.append(StationData(net, code, s.get("location", "00"), prefix,
                               s["longitude"], s["latitude"],
                               station_sensitivity(s), data, valid, len(comps)))
    missing = [s["station"] for s in stations if s["station"] not in seen]
    if missing:
        log.info("no data this chunk for: %s", ", ".join(missing))
    return out


def highpass(x: np.ndarray, freq_hz: float) -> np.ndarray:
    """obspy Trace.filter('highpass', freq) equivalent: causal 4-pole
    Butterworth, one pass (zerophase=False). Verified to reproduce the
    notebook's filtered input to ~1e-6."""
    from scipy.signal import butter, sosfilt

    sos = butter(4, freq_hz, btype="highpass", fs=FS, output="sos")
    return sosfilt(sos, x, axis=-1)


def preprocess(data: np.ndarray, valid: np.ndarray, highpass_hz: float):
    """Notebook preprocessing (demean, highpass) with gaps held at 0."""
    x = data.astype(np.float64).copy()
    x[:, ~valid] = 0.0
    if valid.any():
        x -= x[:, valid].mean(axis=1, keepdims=True)
        x[:, ~valid] = 0.0
    if highpass_hz > 0:
        x = highpass(x, highpass_hz)
        x[:, ~valid] = 0.0
    return x


# ── model ─────────────────────────────────────────────────────────────
def load_model(params: Params):
    import torch
    from phaseno_model import PhaseNO

    if params.device == "auto":
        dev = "mps" if torch.backends.mps.is_available() else \
              "cuda" if torch.cuda.is_available() else "cpu"
    else:
        dev = params.device
    model = PhaseNO.load_from_checkpoint(str(params.checkpoint), map_location="cpu")
    model.eval()
    log.info("PhaseNO loaded on %s", dev)
    return model.to(dev), dev


def predict_chunk(model, dev, X_all: np.ndarray, coords: np.ndarray,
                  edges: np.ndarray) -> np.ndarray:
    """Sliding-window inference over [N, 3, nt] (N >= 2); returns merged
    probabilities [N, 2, nt] (nanmean over overlapping windows)."""
    import torch

    n_sta, _, nt = X_all.shape
    if n_sta < 2:
        raise ValueError("PhaseNO needs >= 2 stations in a chunk")
    starts = list(range(0, nt - IN_SAMPLES + 1, STEP))
    if starts and starts[-1] + IN_SAMPLES < nt:
        starts.append(nt - IN_SAMPLES)
    coverage = int(np.ceil(IN_SAMPLES / STEP + 1))
    merged = np.full((n_sta, 2, nt, coverage), np.nan, dtype=np.float32)
    edge_t = torch.tensor(edges, device=dev)
    with torch.no_grad():
        for wi, s0 in enumerate(starts):
            seg = X_all[:, :, s0:s0 + IN_SAMPLES]
            mu = seg.mean(-1, keepdims=True)
            sd = seg.std(-1, keepdims=True)
            seg = (seg - mu) / (sd + EPS)
            X = np.zeros((n_sta, 5, IN_SAMPLES), dtype=np.float32)
            X[:, :3] = seg / INPUT_SCALE
            X[:, 3] = coords[:, 0:1]
            X[:, 4] = coords[:, 1:2]
            out = torch.sigmoid(model.forward(
                (torch.tensor(X, device=dev), None, edge_t)))
            merged[:, :, s0:s0 + IN_SAMPLES, wi % coverage] = out.cpu().numpy()
    with np.errstate(all="ignore"):
        return np.nanmean(merged, axis=-1)


# ── picks ─────────────────────────────────────────────────────────────
def detect_peaks(x, mph=None, mpd=1):
    """Upstream picker (notebook cell 'codes for picking', itself from
    PhaseNet; Marcos Duarte, MIT). Rising-edge local maxima >= mph,
    at least mpd samples apart (higher peak wins), excluding the
    first/last sample. Returns (indices, heights)."""
    x = np.atleast_1d(x).astype("float64")
    if x.size < 3:
        return np.array([], dtype=int), np.array([])
    dx = x[1:] - x[:-1]
    indnan = np.where(np.isnan(x))[0]
    if indnan.size:
        x[indnan] = np.inf
        dx[np.where(np.isnan(dx))[0]] = np.inf
    ind = np.where((np.hstack((dx, 0)) <= 0) & (np.hstack((0, dx)) > 0))[0]
    if ind.size and indnan.size:
        ind = ind[np.in1d(ind, np.unique(np.hstack((indnan, indnan - 1, indnan + 1))),
                          invert=True)]
    if ind.size and ind[0] == 0:
        ind = ind[1:]
    if ind.size and ind[-1] == x.size - 1:
        ind = ind[:-1]
    if ind.size and mph is not None:
        ind = ind[x[ind] >= mph]
    if ind.size and mpd > 1:
        ind = ind[np.argsort(x[ind])][::-1]
        idel = np.zeros(ind.size, dtype=bool)
        for i in range(ind.size):
            if not idel[i]:
                idel = idel | (ind >= ind[i] - mpd) & (ind <= ind[i] + mpd)
                idel[i] = 0
        ind = np.sort(ind[~idel])
    return ind, x[ind]


def extract_picks(prob: np.ndarray, threshold: float, mpd: int):
    return detect_peaks(np.nan_to_num(prob), mph=threshold, mpd=mpd)


def gap_guard(valid: np.ndarray, guard_samples: int) -> np.ndarray:
    """False within guard_samples of any invalid sample."""
    if valid.all():
        return valid
    k = np.ones(2 * guard_samples + 1, dtype=int)
    return ~(np.convolve((~valid).astype(int), k, mode="same") > 0)


def amplitude_at(vel: np.ndarray, idx: int, next_idx: int | None,
                 window_s: float) -> float:
    """Upstream extract_amplitude: max |v| over components from the pick
    to min(pick + window, next pick)."""
    end = idx + int(window_s * FS)
    if next_idx is not None:
        end = min(end, next_idx)
    seg = vel[:, idx:max(end, idx + 1)]
    return float(np.abs(seg).max()) if seg.size else 0.0


def dedup_across_seams(picks: list[dict], mpd_s: float) -> list[dict]:
    """Re-apply the peak-spacing rule per station/phase across chunk
    seams: of two picks closer than mpd, keep the higher probability."""
    from datetime import datetime

    by_key: dict[tuple, list[dict]] = {}
    for p in picks:
        by_key.setdefault((p["station"], p["phase"]), []).append(p)
    keep: list[dict] = []
    for _, group in by_key.items():
        group.sort(key=lambda p: -float(p["probability"]))
        chosen: list[float] = []
        for p in group:
            t = datetime.fromisoformat(p["time"]).timestamp()
            if all(abs(t - c) > mpd_s for c in chosen):
                chosen.append(t)
                keep.append(p)
    keep.sort(key=lambda r: (r["time"], r["station"], r["phase"]))
    return keep


# ── day driver ────────────────────────────────────────────────────────
def process_day(day_dir: Path, stations: list[dict], out_csv: Path,
                params: Params, day_start=None) -> int:
    """Pick one UTC day. Returns the number of picks written. Raises
    DayError (and writes nothing) when the day cannot be processed."""
    from obspy import UTCDateTime

    day_dir = Path(day_dir).resolve()
    if not day_dir.is_dir():
        raise DayError(f"day directory does not exist: {day_dir}")
    if day_start is None:
        try:
            year, jday = int(day_dir.parent.name), int(day_dir.name)
        except ValueError as exc:
            raise DayError(f"cannot infer YYYY/JJJ from {day_dir}") from exc
        day_start = UTCDateTime(year=year, julday=jday)
    day_end = day_start + 86400
    if round(params.pad_s * FS) % STEP:
        raise DayError(f"pad_s={params.pad_s} must be a multiple of {STEP / FS:g} s")

    center = network_center(stations)
    model, dev = load_model(params)
    chunk_s = params.chunk_hours * 3600
    guard = int(params.gap_guard_s * FS)
    picks: list[dict] = []
    stations_seen: set[str] = set()
    failures: list[str] = []
    t_start = time.monotonic()

    c0 = day_start
    while c0 < day_end:
        c1 = min(c0 + chunk_s, day_end)
        l0, l1 = max(c0 - params.pad_s, day_start), min(c1 + params.pad_s, day_end)
        try:
            sds = load_day(day_dir, stations, l0, l1, params)
            need = params.min_valid_fraction * int(round((l1 - l0) * FS))
            dropped = [s.station for s in sds if s.valid.sum() < need]
            sds = [s for s in sds if s.valid.sum() >= need]
            if dropped:
                log.info("chunk %s: dropped (mostly gap): %s",
                         c0.strftime("%H:%M"), ", ".join(dropped))
            if len(sds) < 2:
                log.warning("chunk %s: %d station(s) with data — PhaseNO "
                            "needs >= 2, skipping", c0.strftime("%H:%M"), len(sds))
                c0 = c1
                continue
            stations_seen.update(s.station for s in sds)
            coords = normalize_coords([s.lon for s in sds], [s.lat for s in sds], center)
            edges = build_edges(coords, params.dis_range_km)
            X = np.stack([preprocess(s.data, s.valid, params.highpass_hz) for s in sds])
            A = np.stack([preprocess(s.data, s.valid, params.amp_highpass_hz) for s in sds])
            prob = predict_chunk(model, dev, X, coords, edges)
        except Exception as exc:
            log.error("chunk %s failed: %s: %s", c0.strftime("%H:%M"),
                      type(exc).__name__, exc)
            failures.append(c0.strftime("%H:%M"))
            c0 = c1
            continue

        off0 = int(round((c0 - l0) * FS))
        off1 = int(round((c1 - l0) * FS))
        for k, s in enumerate(sds):
            ok = gap_guard(s.valid, guard)
            for pi, (ph, thr, win) in enumerate([
                    ("P", params.p_threshold, params.amp_window_p_s),
                    ("S", params.s_threshold, params.amp_window_s_s)]):
                p = prob[k, pi].copy()
                p[~ok] = 0.0
                idx, hts = extract_picks(p, thr, params.min_peak_distance)
                keep = [(int(i), float(h)) for i, h in zip(idx, hts) if off0 <= i < off1]
                for n_, (i, h) in enumerate(keep):
                    nxt = keep[n_ + 1][0] if n_ + 1 < len(keep) else None
                    amp = amplitude_at(A[k], i, nxt, win)
                    t = l0 + i / FS
                    picks.append({
                        "network": s.network, "station": s.station,
                        "location": s.location, "channel": s.chan_prefix + "Z",
                        "phase": ph,
                        "time": t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                        "probability": f"{h:.4f}", "model": MODEL_TAG,
                        "amplitude": f"{amp:.6e}" if amp > 0 else "",
                        "amplitude_channel": s.chan_prefix + "Z" if amp > 0 else "",
                    })
        log.info("chunk %s-%s: %d stations, %d picks so far",
                 c0.strftime("%H:%M"), c1.strftime("%H:%M"), len(sds), len(picks))
        c0 = c1

    if failures:
        raise DayError(f"{len(failures)} chunk(s) failed: {', '.join(failures)}")
    if not stations_seen:
        raise DayError(f"no station data found in {day_dir}")

    picks = dedup_across_seams(picks, params.min_peak_distance / FS)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(picks)
    log.info("wrote %d picks (%d stations) -> %s (%.1f min)", len(picks),
             len(stations_seen), out_csv, (time.monotonic() - t_start) / 60)
    return len(picks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day-dir", required=True, type=Path,
                    help="pipeline day directory (.../YYYY/JJJ) of mseed files")
    ap.add_argument("--stations", required=True, type=Path, help="stations.json")
    ap.add_argument("--out", required=True, type=Path, help="picks CSV to write")
    ap.add_argument("--input-units", choices=["velocity", "counts"], default="velocity",
                    help="'counts' for 1-raw (divide by sensitivity), 'velocity' for 2-processed")
    ap.add_argument("--p-threshold", type=float, default=Params.p_threshold)
    ap.add_argument("--s-threshold", type=float, default=Params.s_threshold)
    ap.add_argument("--dis-range-km", type=float, default=Params.dis_range_km)
    ap.add_argument("--highpass", type=float, default=Params.highpass_hz)
    ap.add_argument("--device", default="auto", help="auto|mps|cuda|cpu")
    ap.add_argument("--checkpoint", type=Path, default=Params.checkpoint)
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.out.exists() and not a.force:
        log.info("%s exists — skipping (use --force to redo)", a.out)
        return 0
    params = Params(checkpoint=a.checkpoint, device=a.device,
                    p_threshold=a.p_threshold, s_threshold=a.s_threshold,
                    highpass_hz=a.highpass, dis_range_km=a.dis_range_km,
                    input_units=a.input_units)
    stations = json.load(open(a.stations))
    try:
        process_day(a.day_dir, stations, a.out, params)
    except DayError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
