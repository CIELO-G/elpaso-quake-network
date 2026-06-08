"""Waveform retrieval + spectrogram rendering endpoints.

Hot path for the review UI — loading a day's mseed files, slicing to the
event window, optionally bandpass-filtering, and rendering a small
spectrogram PNG inline. Streams are cached LRU-by-path so re-opening an
event in review mode doesn't re-read the daily file.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from dashboard.deps import (
    OUTPUT_DIR,
    PICKS_DIR,
    PROCESSED_DIR,
    file_mtime,
    load_stations,
    read_assignments,
    read_catalog,
)

router = APIRouter()

# ── Stream cache ─────────────────────────────────────────────────
# LRU keyed on (path, mtime) so a file edited under us invalidates cleanly.
# A single review-mode load pulls ~14 stations × 3 channels = 42 mseed files,
# so the default cap is sized to hold one full event without churning. Env
# override for users with very large networks.
_waveform_cache: dict[str, tuple[float, object]] = {}
_WAVEFORM_CACHE_MAX = int(os.environ.get("WAVEFORM_CACHE_MAX", "100"))
_WAVEFORM_MAX_DURATION = 600  # seconds (10 min hard cap on a single request)

# ── Spectrogram disk cache ───────────────────────────────────────
# Spectrograms are pure functions of (trace data + window + filter + size),
# all knobs captured in the cache key. So cached PNGs never go stale for a
# given event window — they only need recomputing if the underlying mseed
# changes (rare; recompute on demand by deleting the cache dir).
_SPEC_CACHE_DIR = OUTPUT_DIR / "cache" / "spectrograms"

# Eviction ceiling — without this the cache grows monotonically forever
# (one PNG per unique event_id+station+channel+window+filter+size key).
# Configurable via SPECTROGRAM_CACHE_MAX_MB; check every N saves to amortize cost.
_SPEC_CACHE_MAX_BYTES = int(os.environ.get("SPECTROGRAM_CACHE_MAX_MB", "1024")) * 1024 * 1024
_SPEC_CACHE_CHECK_EVERY = 50
_spec_save_counter = 0


def _evict_spec_cache_if_over_quota() -> None:
    """Drop oldest-mtime PNGs until cache is under the quota (down to 90%)."""
    if not _SPEC_CACHE_DIR.exists():
        return
    try:
        files = []
        total = 0
        for f in _SPEC_CACHE_DIR.iterdir():
            if not f.is_file() or not f.name.endswith(".png"):
                continue
            try:
                st = f.stat()
                files.append((st.st_mtime, st.st_size, f))
                total += st.st_size
            except OSError:
                continue
        if total <= _SPEC_CACHE_MAX_BYTES:
            return
        # Free down to 90% of quota so we don't evict on every save once full
        target_free = total - int(_SPEC_CACHE_MAX_BYTES * 0.9)
        files.sort(key=lambda t: t[0])  # oldest first
        for _, sz, path in files:
            if target_free <= 0:
                break
            try:
                path.unlink()
                target_free -= sz
            except OSError:
                pass
    except OSError:
        pass


def _spec_cache_key(
    *,
    event_id: str,
    network: str,
    station: str,
    channel: str,
    window_before: float,
    window_after: float,
    freqmin: Optional[float],
    freqmax: Optional[float],
    width: int,
    height: int,
) -> str:
    """Deterministic short filename stem for the spectrogram cache."""
    parts = (
        event_id, network, station, channel,
        f"{window_before:g}", f"{window_after:g}",
        f"{freqmin or 0:g}", f"{freqmax or 0:g}",
        str(width), str(height),
    )
    h = hashlib.md5("|".join(parts).encode()).hexdigest()[:16]
    # event_id prefix gives a friendly path; hash disambiguates the knobs
    return f"{event_id}_{station}_{channel}_{h}"


def _load_cached_spec(key: str) -> Optional[str]:
    """Return cached base64 PNG if present, else None."""
    path = _SPEC_CACHE_DIR / f"{key}.png"
    if not path.exists():
        return None
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _save_cached_spec(key: str, b64: str) -> None:
    """Best-effort: write base64-decoded PNG to the cache dir."""
    global _spec_save_counter
    if not b64:
        return
    try:
        _SPEC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_SPEC_CACHE_DIR / f"{key}.png").write_bytes(base64.b64decode(b64))
    except OSError:
        pass  # missing cache is not an error; we'll just recompute next time
    _spec_save_counter += 1
    if _spec_save_counter >= _SPEC_CACHE_CHECK_EVERY:
        _spec_save_counter = 0
        _evict_spec_cache_if_over_quota()


def _spectrogram_or_cached(
    data: list[float],
    sampling_rate: float,
    width: int,
    height: int,
    *,
    cache_key: Optional[str] = None,
) -> str:
    """Compute spectrogram, hitting/populating disk cache when ``cache_key`` is given.

    Falls back to direct compute (no caching) when ``cache_key`` is None —
    used by the generic Waveviewer endpoint where each request is for an
    arbitrary time window with low hit-rate.
    """
    if not cache_key:
        return _compute_spectrogram_png(data, sampling_rate, width, height)
    cached = _load_cached_spec(cache_key)
    if cached is not None:
        return cached
    b64 = _compute_spectrogram_png(data, sampling_rate, width, height)
    _save_cached_spec(cache_key, b64)
    return b64


def _get_obspy_stream(file_path: Path):
    """Read a miniSEED file, caching the parsed ``Stream``."""
    key = str(file_path)
    mtime = file_mtime(file_path)
    if key in _waveform_cache:
        cached_mtime, cached_stream = _waveform_cache[key]
        if cached_mtime == mtime:
            return cached_stream
    try:
        from obspy import read as obspy_read
        st = obspy_read(str(file_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read {file_path.name}: {exc}")
    if len(_waveform_cache) >= _WAVEFORM_CACHE_MAX:
        oldest_key = next(iter(_waveform_cache))
        del _waveform_cache[oldest_key]
    _waveform_cache[key] = (mtime, st)
    return st


def _compute_spectrogram_png(
    data: list[float],
    sampling_rate: float,
    width: int = 800,
    height: int = 128,
) -> str:
    """Compute a small viridis spectrogram and return as Base64-encoded PNG.

    Avoids the matplotlib import cost by mapping log-power to a 5-stop
    viridis LUT manually, then resizing with PIL bilinear interpolation.
    """
    import base64
    import io
    import numpy as np
    from PIL import Image
    from scipy.signal import spectrogram as sp_spectrogram

    arr = np.array(data, dtype=np.float64)
    if len(arr) < 32:
        return ""

    nperseg = min(128, len(arr) // 4)
    noverlap = int(nperseg * 0.75)
    f, t, Sxx = sp_spectrogram(arr, fs=sampling_rate, nperseg=nperseg, noverlap=noverlap)

    # Log-power, clipped to a usable dynamic range
    Sxx_log = 10 * np.log10(Sxx + 1e-30)
    vmin = np.percentile(Sxx_log, 5)
    vmax = np.percentile(Sxx_log, 99)
    Sxx_norm = np.clip((Sxx_log - vmin) / (vmax - vmin + 1e-10), 0, 1)

    viridis_lut = np.array([
        [68, 1, 84],    [59, 82, 139],  [33, 145, 140],
        [94, 201, 98],  [253, 231, 37],
    ], dtype=np.uint8)
    idx = (Sxx_norm * (len(viridis_lut) - 1)).astype(int)
    img = viridis_lut[idx[::-1, :]]  # high freq at top

    pil_img = Image.fromarray(img, mode="RGB").resize((width, height), Image.BILINEAR)
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── Helpers for picking the right mseed file ─────────────────────
def _find_mseed(
    day_dir: Path, network: str, station: str, channel: str
) -> list[Path]:
    """Return mseed candidates matching ``net.sta.*.chan.*.mseed``.

    Falls back to ``net.sta.*.?<chan[1:]>.*.mseed`` so a request for EHZ
    will also find HHZ (broadband stations) when the exact code is absent.
    """
    if not day_dir.exists():
        return []
    candidates = list(day_dir.glob(f"{network}.{station}.*.{channel}.*.mseed"))
    if not candidates and len(channel) == 3:
        candidates = list(day_dir.glob(f"{network}.{station}.*.?{channel[1:]}.*.mseed"))
    return candidates


# ── Single-trace endpoint ────────────────────────────────────────
@router.get("/api/waveform")
async def waveform(
    network: str = Query(...),
    station: str = Query(...),
    channel: str = Query(default="EHZ"),
    location: str = Query(default="00"),
    start: str = Query(..., description="ISO 8601 start time"),
    end: str = Query(..., description="ISO 8601 end time"),
    max_samples: int = Query(default=10000, ge=100, le=100000),
):
    """Single-trace waveform from the processed mseed tree."""
    from obspy import UTCDateTime

    t_start = UTCDateTime(start)
    t_end = UTCDateTime(end)
    duration = t_end - t_start
    if duration <= 0 or duration > _WAVEFORM_MAX_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Duration must be 0-{_WAVEFORM_MAX_DURATION}s, got {duration:.0f}s",
        )

    jday = t_start.julday
    year = t_start.year
    day_dir = PROCESSED_DIR / str(year) / str(jday).zfill(3)
    if not day_dir.exists():
        raise HTTPException(status_code=404, detail=f"No data for {year}/{jday:03d}")

    # Try exact filename first; fall back to glob via _find_mseed.
    pattern = f"{network}.{station}.{location}.{channel}.{year}.{jday:03d}.mseed"
    mseed_file = day_dir / pattern
    if not mseed_file.exists():
        candidates = _find_mseed(day_dir, network, station, channel)
        if not candidates:
            raise HTTPException(
                status_code=404, detail=f"No miniSEED for {network}.{station}.{channel}"
            )
        mseed_file = candidates[0]

    st = _get_obspy_stream(mseed_file)
    st_sliced = st.copy().trim(t_start, t_end)
    if len(st_sliced) == 0:
        raise HTTPException(status_code=404, detail="No data in requested time window")

    tr = st_sliced[0]
    data = tr.data.tolist()
    if len(data) > max_samples:
        step = len(data) // max_samples
        data = data[::step]

    return {
        "data": data,
        "sampling_rate": tr.stats.sampling_rate,
        "starttime": str(tr.stats.starttime),
        "endtime": str(tr.stats.endtime),
        "npts": tr.stats.npts,
        "network": tr.stats.network,
        "station": tr.stats.station,
        "channel": tr.stats.channel,
        "location": tr.stats.location,
    }


# ── All-stations endpoint for the Waveviewer tab ─────────────────
@router.get("/api/waveforms/all")
async def waveforms_all(
    start: str = Query(..., description="ISO 8601 start time"),
    end: str = Query(..., description="ISO 8601 end time"),
    channel: str = Query(default="EHZ"),
    max_samples: int = Query(default=10000, ge=100, le=100000),
    freqmin: Optional[float] = Query(default=None, ge=0.01, le=50.0),
    freqmax: Optional[float] = Query(default=None, ge=0.1, le=50.0),
    spectrogram: bool = Query(default=False),
):
    """Waveforms for ALL stations in an arbitrary time window, with optional
    bandpass filter and inline spectrogram PNG per trace."""
    from obspy import UTCDateTime

    t_start = UTCDateTime(start)
    t_end = UTCDateTime(end)
    duration = t_end - t_start
    if duration <= 0 or duration > _WAVEFORM_MAX_DURATION:
        raise HTTPException(
            status_code=400,
            detail=f"Duration must be 0-{_WAVEFORM_MAX_DURATION}s, got {duration:.0f}s",
        )

    jday = t_start.julday
    year = t_start.year
    day_dir = PROCESSED_DIR / str(year) / str(jday).zfill(3)

    # Load picks for the day and filter to the requested time window.
    picks_file = PICKS_DIR / str(year) / str(jday).zfill(3) / f"{year}.{jday:03d}.picks.csv"
    picks_by_station: dict[str, list[dict]] = {}
    if picks_file.exists():
        start_iso = str(t_start).replace("T", " ").rstrip("Z")
        end_iso = str(t_end).replace("T", " ").rstrip("Z")
        with open(picks_file, newline="") as f:
            for row in csv.DictReader(f):
                pick_time = row.get("time", "")
                if not pick_time:
                    continue
                pt = pick_time.replace("T", " ")
                if pt < start_iso or pt > end_iso:
                    continue
                sta_key = f"{row.get('network', '')}.{row.get('station', '')}"
                picks_by_station.setdefault(sta_key, []).append({
                    "phase": row.get("phase", ""),
                    "time": pick_time,
                    "probability": float(row["probability"]) if row.get("probability") else None,
                    "channel": row.get("channel", ""),
                })

    traces = _build_traces_for_window(
        day_dir, t_start, t_end,
        channel=channel,
        max_samples=max_samples,
        freqmin=freqmin, freqmax=freqmax,
        spectrogram=spectrogram,
        picks_by_station=picks_by_station,
        include_amplitude=False,
    )

    return {"start": str(t_start), "end": str(t_end), "traces": traces}


# ── Event-window endpoints ───────────────────────────────────────
@router.get("/api/event/{event_id}/waveforms")
async def event_waveforms(
    event_id: str,
    channel: str = Query(default="EHZ"),
    window_before: float = Query(default=5.0, ge=0, le=60),
    window_after: float = Query(default=30.0, ge=5, le=300),
    max_samples: int = Query(default=5000, ge=100, le=50000),
    spectrogram: bool = Query(default=False),
):
    """Waveforms for stations that contributed picks to this event."""
    from obspy import UTCDateTime

    rows = read_catalog()
    event_row = next((r for r in rows if r.get("event_id") == event_id), None)
    if event_row is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    event_time = event_row.get("time", "")
    if not event_time:
        raise HTTPException(status_code=400, detail="Event has no time")

    assignments = read_assignments()
    event_assignments = [a for a in assignments if a.get("event_id") == event_id]
    if not event_assignments:
        raise HTTPException(status_code=404, detail=f"No pick assignments for {event_id}")

    # Group picks per station that contributed to this event.
    stations_seen: dict[str, list[dict]] = {}
    for a in event_assignments:
        sta_key = f"{a.get('network', '')}.{a.get('station', '')}"
        stations_seen.setdefault(sta_key, []).append({
            "phase": a.get("phase", ""),
            "time": a.get("time", ""),
            "probability": float(a["probability"]) if a.get("probability") else None,
            "channel": a.get("channel", ""),
        })

    t_origin = UTCDateTime(event_time)
    t_start = t_origin - window_before
    t_end = t_origin + window_after
    day_dir = PROCESSED_DIR / str(t_origin.year) / str(t_origin.julday).zfill(3)

    traces = []
    for sta_key, picks in stations_seen.items():
        parts = sta_key.split(".")
        if len(parts) != 2:
            continue
        net, sta = parts

        candidates = _find_mseed(day_dir, net, sta, channel)
        if not candidates:
            traces.append({
                "station": sta, "network": net, "channel": channel,
                "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                "picks": picks, "error": "No miniSEED file found",
            })
            continue

        try:
            st = _get_obspy_stream(candidates[0])
            st_sliced = st.copy().trim(t_start, t_end)
            if len(st_sliced) == 0:
                traces.append({
                    "station": sta, "network": net, "channel": channel,
                    "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                    "picks": picks, "error": "No data in time window",
                })
                continue

            tr = st_sliced[0]

            spec_b64 = ""
            if spectrogram and len(tr.data) >= 32:
                cache_key = _spec_cache_key(
                    event_id=event_id, network=net, station=sta,
                    channel=channel, window_before=window_before,
                    window_after=window_after, freqmin=None, freqmax=None,
                    width=800, height=128,
                )
                spec_b64 = _spectrogram_or_cached(
                    tr.data.tolist(), tr.stats.sampling_rate,
                    width=800, height=128, cache_key=cache_key,
                )

            data = tr.data.tolist()
            if len(data) > max_samples:
                step = len(data) // max_samples
                data = data[::step]

            trace_dict = {
                "station": sta, "network": net, "channel": channel,
                "data": data,
                "sampling_rate": tr.stats.sampling_rate,
                "starttime": str(tr.stats.starttime),
                "endtime": str(tr.stats.endtime),
                "picks": picks,
            }
            if spectrogram and spec_b64:
                trace_dict["spectrogram_b64"] = spec_b64
            traces.append(trace_dict)
        except Exception as exc:
            traces.append({
                "station": sta, "network": net, "channel": channel,
                "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
                "picks": picks, "error": str(exc),
            })

    return {
        "event_id": event_id,
        "event_time": event_time,
        "window_before": window_before,
        "window_after": window_after,
        "traces": traces,
    }


@router.get("/api/event/{event_id}/waveforms/all")
async def event_waveforms_all(
    event_id: str,
    channel: str = Query(default="EHZ"),
    window_before: float = Query(default=10.0, ge=0, le=120),
    window_after: float = Query(default=60.0, ge=5, le=600),
    max_samples: int = Query(default=10000, ge=100, le=100000),
    freqmin: Optional[float] = Query(default=None, ge=0.01, le=50.0),
    freqmax: Optional[float] = Query(default=None, ge=0.1, le=50.0),
    spectrogram: bool = Query(default=False),
):
    """Waveforms for ALL stations around an event (for review-mode pick editing)."""
    from obspy import UTCDateTime

    rows = read_catalog()
    event_row = next((r for r in rows if r.get("event_id") == event_id), None)
    if event_row is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    event_time = event_row.get("time", "")
    if not event_time:
        raise HTTPException(status_code=400, detail="Event has no time")

    # Load existing picks for this event
    picks_by_station: dict[str, list[dict]] = {}
    for a in read_assignments():
        if a.get("event_id") == event_id:
            sta_key = f"{a.get('network', '')}.{a.get('station', '')}"
            picks_by_station.setdefault(sta_key, []).append({
                "phase": a.get("phase", ""),
                "time": a.get("time", ""),
                "probability": float(a["probability"]) if a.get("probability") else None,
                "channel": a.get("channel", ""),
                "amplitude": float(a["amplitude"]) if a.get("amplitude") else None,
            })

    t_origin = UTCDateTime(event_time)
    t_start = t_origin - window_before
    t_end = t_origin + window_after
    day_dir = PROCESSED_DIR / str(t_origin.year) / str(t_origin.julday).zfill(3)

    traces = _build_traces_for_window(
        day_dir, t_start, t_end,
        channel=channel,
        max_samples=max_samples,
        freqmin=freqmin, freqmax=freqmax,
        spectrogram=spectrogram,
        picks_by_station=picks_by_station,
        include_amplitude=True,
        event_id=event_id,
        window_before=window_before,
        window_after=window_after,
    )

    ev_lat = float(event_row["latitude"]) if event_row.get("latitude") else None
    ev_lon = float(event_row["longitude"]) if event_row.get("longitude") else None

    return {
        "event_id": event_id,
        "event_time": event_time,
        "event_latitude": ev_lat,
        "event_longitude": ev_lon,
        "traces": traces,
    }


# ── Shared per-station trace builder ─────────────────────────────
def _build_traces_for_window(
    day_dir: Path,
    t_start,
    t_end,
    *,
    channel: str,
    max_samples: int,
    freqmin: Optional[float],
    freqmax: Optional[float],
    spectrogram: bool,
    picks_by_station: dict[str, list[dict]],
    include_amplitude: bool,
    event_id: Optional[str] = None,
    window_before: float = 0.0,
    window_after: float = 0.0,
) -> list[dict]:
    """Fan out a time window across every station and every requested channel.

    Used by both ``/api/waveforms/all`` (no caching — windows are arbitrary)
    and ``/api/event/{id}/waveforms/all`` (cached spectrograms keyed on the
    event_id + window + filter combo).
    """
    channels_to_load = ["EHZ", "EHN", "EHE"] if channel == "3C" else [channel]

    # Flatten (station, channel) into a task list so we can fan out across
    # threads. Order preservation matters (frontend expects stable trace
    # order for stacked-station plots), so we keep the list ordering and
    # use ThreadPoolExecutor.map() which yields in submission order.
    tasks: list[tuple[dict, str]] = []
    for s in load_stations():
        for chan in channels_to_load:
            tasks.append((s, chan))

    def _load_one(task: tuple[dict, str]) -> dict:
        s, chan = task
        net = s["network"]
        sta = s["station"]
        sta_lat = s.get("latitude")
        sta_lon = s.get("longitude")
        picks = picks_by_station.get(f"{net}.{sta}", [])
        miss = {
            "station": sta, "network": net, "channel": chan,
            "latitude": sta_lat, "longitude": sta_lon,
            "data": [], "sampling_rate": 0, "starttime": "", "endtime": "",
            "picks": picks, "has_data": False,
        }

        candidates = _find_mseed(day_dir, net, sta, chan)
        if not candidates:
            return {**miss, "error": "No miniSEED file found"}

        try:
            stream = _get_obspy_stream(candidates[0])
            st_sliced = stream.copy().trim(t_start, t_end)
            if len(st_sliced) == 0:
                return {**miss, "error": "No data in time window"}

            tr = st_sliced[0]
            if freqmin is not None and freqmax is not None:
                tr = tr.copy()
                tr.detrend("demean")
                tr.filter("bandpass", freqmin=freqmin, freqmax=freqmax, corners=4, zerophase=True)
            elif freqmin is not None:
                tr = tr.copy()
                tr.detrend("demean")
                tr.filter("highpass", freq=freqmin, corners=4, zerophase=True)
            elif freqmax is not None:
                tr = tr.copy()
                tr.detrend("demean")
                tr.filter("lowpass", freq=freqmax, corners=4, zerophase=True)

            spec_b64 = ""
            if spectrogram and len(tr.data) >= 32:
                cache_key = None
                if event_id is not None:
                    cache_key = _spec_cache_key(
                        event_id=event_id, network=net, station=sta,
                        channel=tr.stats.channel,
                        window_before=window_before,
                        window_after=window_after,
                        freqmin=freqmin, freqmax=freqmax,
                        width=1200, height=160,
                    )
                spec_b64 = _spectrogram_or_cached(
                    tr.data.tolist(), tr.stats.sampling_rate,
                    width=1200, height=160, cache_key=cache_key,
                )

            data = tr.data.tolist()
            if len(data) > max_samples:
                step = len(data) // max_samples
                data = data[::step]

            trace_dict = {
                "station": sta, "network": net, "channel": tr.stats.channel,
                "latitude": sta_lat, "longitude": sta_lon,
                "data": data,
                "sampling_rate": tr.stats.sampling_rate,
                "starttime": str(tr.stats.starttime),
                "endtime": str(tr.stats.endtime),
                "picks": picks,
                "has_data": True,
            }
            if spectrogram and spec_b64:
                trace_dict["spectrogram_b64"] = spec_b64
            return trace_dict
        except Exception as exc:
            return {**miss, "error": str(exc)}

    # Worker pool: mseed read + filter + spectrogram are FFT/numpy-heavy
    # (GIL-releasing). Cap at cpu_count; nothing to gain from over-subscribing.
    n_workers = min(os.cpu_count() or 1, max(1, len(tasks)))
    with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="trace") as ex:
        return list(ex.map(_load_one, tasks))
