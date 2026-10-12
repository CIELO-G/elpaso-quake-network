#!/usr/bin/env python
"""Import a temporary deployment (local miniSEED, no FDSN) into the archive.

The daily ingest fetches per-station data from FDSN and caches StationXML.
A temporary deployment arrives differently: a pile of miniSEED files (often
one file per hour holding every station), a station list with coordinates
and a sensitivity, no StationXML. This tool turns that into exactly what
the rest of the pipeline expects, so steps 2-5 run unchanged:

  output/1-raw/YYYY/JJJ/NET.STA.LOC.CHA.YYYY.JJJ.mseed   one file per station-channel-day
  output/1-metadata/NET.STA.xml                          StationXML with a velocity response
  stations.json                                          entries with start_date AND end_date

Input layouts (detected from the first file's headers):
  * hourly all-station files (one file = every station for an hour) — buffered per
    UTC day and written once per day;
  * per-station files (SAC or miniSEED, one station-channel per file, any span)
    — handled one station at a time, in parallel (``--workers``).
Rates: a near-nominal clock (999.99994 Hz) is snapped to nominal; ``--target-rate``
decimates by an integer factor with ObsPy's anti-alias lowpass. Integer counts
stay integer (INT32 Steim2); anything else is stored float32.

Station list: CSV with columns  station,lat,lon,elv(m),unit,componet,response
(the PhaseNet "stations.csv" shape; ``station`` is NET.STA.LOC.BAND, e.g.
4B.101..GP; ``response`` is counts per m/s, one value per component).

Usage
-----
    python scripts/import_deployment.py \\
        --data /Volumes/VelascoRAID1/Data/ElPaso/deployments/ElPaso2021_nodal/data \\
        --station-list /Volumes/VelascoRAID1/Data/ElPaso/deployments/ElPaso2021_nodal/Station_list.csv \\
        --deployment ElPaso2021 --model "SmartSolo node" [--geophone-hz 5] \\
        [--dry-run] [--only-stations] [--only-waveforms]

Then: python scripts/nlloc_build_grids.py, and run steps 2-5 for the dates
(see RUNBOOK.md "Importing a temporary deployment").
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from obspy import Stream, UTCDateTime, read
from obspy.core.inventory import Channel, Inventory, Network, Site, Station
from obspy.core.inventory.response import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.atomic_io import atomic_write_stream, atomic_write_text  # noqa: E402
from lib.models import StationValidationError, validate_stations  # noqa: E402

STATIONS_FILE = ROOT / "stations.json"
RAW_DIR = ROOT / "output" / "1-raw"
META_DIR = ROOT / "output" / "1-metadata"

NOMINAL_RATE_TOL = 0.01  # Hz: snap 99.99999 Hz node clocks to 100.0


# ── station list ─────────────────────────────────────────────────────────


def read_station_list(path: Path) -> dict[str, dict]:
    """{NET.STA: {network, station, location, band, lat, lon, elev, components, sensitivity}}"""
    out: dict[str, dict] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            net, sta, loc, band = row["station"].split(".")
            comps = [c.strip() for c in row["componet"].split(",")]
            resp = [float(x) for x in row["response"].split(",")]
            out[f"{net}.{sta}"] = {
                "network": net, "station": sta, "location": loc, "band": band,
                "latitude": float(row["lat"]), "longitude": float(row["lon"]),
                "elevation_m": float(row["elv(m)"]), "unit": row["unit"].strip(),
                "components": comps, "sensitivity": float(np.median(resp)),
            }
    return out


def geophone_response(sensitivity: float, f0_hz: float | None, damping: float = 0.7,
                      norm_freq: float = 10.0) -> Response:
    """Velocity response: flat (sensitivity only) or a geophone with corner f0."""
    if not f0_hz:
        return Response.from_paz(
            zeros=[], poles=[], stage_gain=sensitivity, stage_gain_frequency=norm_freq,
            input_units="M/S", output_units="COUNTS", normalization_frequency=norm_freq,
        )
    w0 = 2 * math.pi * f0_hz
    poles = [complex(-damping * w0, w0 * math.sqrt(1 - damping**2)),
             complex(-damping * w0, -w0 * math.sqrt(1 - damping**2))]
    return Response.from_paz(
        zeros=[0j, 0j], poles=poles, stage_gain=sensitivity, stage_gain_frequency=norm_freq,
        input_units="M/S", output_units="COUNTS", normalization_frequency=norm_freq,
    )


def build_inventory(meta: dict, channels: list[str], start: UTCDateTime, end: UTCDateTime,
                    f0_hz: float | None, rate: float) -> Inventory:
    chans = []
    for code in channels:
        ch = Channel(
            code=code, location_code=meta["location"], latitude=meta["latitude"],
            longitude=meta["longitude"], elevation=meta["elevation_m"], depth=0.0,
            sample_rate=rate, start_date=start, end_date=end,
            azimuth=0.0, dip=-90.0 if code.endswith("Z") else 0.0,
        )
        ch.response = geophone_response(meta["sensitivity"], f0_hz)
        chans.append(ch)
    sta = Station(
        code=meta["station"], latitude=meta["latitude"], longitude=meta["longitude"],
        elevation=meta["elevation_m"], channels=chans, start_date=start, end_date=end,
        site=Site(name=f"{meta['network']}.{meta['station']} temporary node"),
    )
    net = Network(code=meta["network"], stations=[sta], start_date=start, end_date=end)
    return Inventory(networks=[net], source="scripts/import_deployment.py")


# ── waveforms ────────────────────────────────────────────────────────────


def normalise_trace(tr, target_rate: float | None = None, rate_tol: float = NOMINAL_RATE_TOL) -> bool:
    """Snap a near-nominal clock to nominal, decimate to ``target_rate`` if
    asked, and pick the storage type. Returns True when the samples are
    integer counts (stored INT32/Steim2), False when stored float32."""
    nominal = round(tr.stats.sampling_rate)
    if abs(tr.stats.sampling_rate - nominal) <= rate_tol:
        tr.stats.sampling_rate = float(nominal)
    integral = bool(np.all(np.mod(tr.data, 1) == 0)) if tr.data.size else True
    if target_rate and tr.stats.sampling_rate != target_rate:
        ratio = tr.stats.sampling_rate / target_rate
        if abs(ratio - round(ratio)) > 1e-6 or ratio < 1:
            raise ValueError(f"{tr.id}: cannot decimate {tr.stats.sampling_rate} Hz to {target_rate} Hz by an integer factor")
        factor = int(round(ratio))
        # ObsPy applies a zero-phase Chebyshev II lowpass below the new Nyquist
        # before picking every factor-th sample (no aliasing); factor > 16 is
        # done in two passes as ObsPy requires.
        while factor > 1:
            step = min(factor, 16)
            tr.decimate(step, no_filter=False, strict_length=False)
            factor //= step
    if integral:
        tr.data = np.round(tr.data).astype(np.int32)
    else:
        tr.data = tr.data.astype(np.float32)
    return integral


def write_day_piece(piece, year: int, jday: int, integral: bool, raw_dir: Path) -> Path:
    """Merge one trace piece into its archive day file (read-merge-write)."""
    day_dir = raw_dir / str(year) / f"{jday:03d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    net, sta, loc, cha = piece.id.split(".")
    fpath = day_dir / f"{net}.{sta}.{loc}.{cha}.{year}.{jday:03d}.mseed"
    s = Stream([piece])
    if fpath.exists():
        s += read(str(fpath))
    s.merge(method=1)  # identical overlaps (duplicated export files) collapse
    s = s.split()
    atomic_write_stream(s, fpath, format="MSEED", encoding="STEIM2" if integral else "FLOAT32")
    return fpath


def import_station_files(station_key: str, files: list[Path], target_rate: float | None, raw_dir: Path) -> dict:
    """Per-station layout: one worker imports every file of one station.

    Files of one station only ever touch that station's archive files, so
    stations can run in parallel without write conflicts."""
    stats = {"station": station_key, "files": 0, "pieces": 0, "duplicates": 0,
             "channels": set(), "first": None, "last": None, "integral": None}
    seen: dict[str, list[tuple]] = defaultdict(list)
    for fp in sorted(files):
        for tr in read(str(fp)):
            if f"{tr.stats.network}.{tr.stats.station}" != station_key:
                continue
            t0, t1 = tr.stats.starttime, tr.stats.endtime
            key = tr.id
            if any(abs(t0 - a) < 1.0 and abs(t1 - b) < 1.0 for a, b in seen[key]):
                stats["duplicates"] += 1  # same window already imported (duplicated file)
                continue
            seen[key].append((t0, t1))
            integral = normalise_trace(tr, target_rate)
            stats["integral"] = integral
            stats["channels"].add(tr.stats.channel)
            stats["first"] = t0 if stats["first"] is None else min(stats["first"], t0)
            stats["last"] = t1 if stats["last"] is None else max(stats["last"], t1)
            day = UTCDateTime(t0.year, t0.month, t0.day)
            while day <= tr.stats.endtime:
                piece = tr.slice(max(day, tr.stats.starttime), min(day + 86400 - tr.stats.delta, tr.stats.endtime))
                if piece.stats.npts > 0:
                    write_day_piece(piece, day.year, day.julday, integral, raw_dir)
                    stats["pieces"] += 1
                day += 86400
        stats["files"] += 1
    return stats


def day_key(t: UTCDateTime) -> tuple[int, int]:
    return t.year, t.julday


def import_waveforms(files: list[Path], wanted: set[str], dry_run: bool, log, target_rate=None, raw_dir: Path = RAW_DIR) -> dict:
    """Write per-station-channel-day files. Files are processed in time order
    and each UTC day is flushed once all of its hourly inputs have been read,
    so memory holds at most two days of one deployment."""
    stats = {"files": 0, "traces": 0, "days": 0, "written": 0, "skipped_ids": set(),
             "first": None, "last": None, "channels": set(), "rate": None}
    buckets: dict[tuple[int, int], Stream] = defaultdict(Stream)

    def flush(day):
        st = buckets.pop(day)
        if dry_run:
            stats["days"] += 1
            return
        by_id: dict[str, Stream] = defaultdict(Stream)
        for tr in st:
            by_id[tr.id].append(tr)
        year, jday = day
        day_dir = raw_dir / str(year) / f"{jday:03d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        for tid, s in by_id.items():
            net, sta, loc, cha = tid.split(".")
            fpath = day_dir / f"{net}.{sta}.{loc}.{cha}.{year}.{jday:03d}.mseed"
            if fpath.exists():
                s += read(str(fpath))
            s.merge(method=1)
            s = s.split()
            enc = "STEIM2" if stats.get("integral") else "FLOAT32"
            atomic_write_stream(s, fpath, format="MSEED", encoding=enc)
            stats["written"] += 1
        stats["days"] += 1
        log(f"  day {year}/{jday:03d}: {len(by_id)} files")

    for i, fp in enumerate(sorted(files), 1):
        st = read(str(fp))
        stats["files"] += 1
        for tr in st:
            key = f"{tr.stats.network}.{tr.stats.station}"
            if key not in wanted:
                stats["skipped_ids"].add(key)
                continue
            stats["integral"] = normalise_trace(tr, target_rate)
            stats["rate"] = tr.stats.sampling_rate
            stats["channels"].add(tr.stats.channel)
            stats["traces"] += 1
            t0, t1 = tr.stats.starttime, tr.stats.endtime
            stats["first"] = t0 if stats["first"] is None else min(stats["first"], t0)
            stats["last"] = t1 if stats["last"] is None else max(stats["last"], t1)
            # a trace may straddle midnight: slice per day
            day = UTCDateTime(t0.year, t0.month, t0.day)
            while day <= t1:
                piece = tr.slice(max(day, t0), min(day + 86400 - tr.stats.delta, t1))
                if piece.stats.npts > 0:
                    buckets[day_key(day)].append(piece)
                day += 86400
        # flush any day older than the newest one we are filling
        newest = max(buckets) if buckets else None
        for day in sorted(d for d in buckets if d != newest):
            flush(day)
        if i % 10 == 0 or i == len(files):
            log(f"[{i}/{len(files)}] {fp.name}")
    for day in sorted(buckets):
        flush(day)
    return stats


# ── stations.json ────────────────────────────────────────────────────────


def station_entry(meta: dict, args, start: date, end: date, sensor_kind: str) -> dict:
    return {
        "network": meta["network"],
        "station": meta["station"],
        "location": meta["location"],
        "channels": f"{meta['band']}?",
        "latitude": meta["latitude"],
        "longitude": meta["longitude"],
        "elevation_m": meta["elevation_m"],
        "sample_rate_hz": 100.0,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "model": args.model,
        "deployment": args.deployment,
        "instrument": {
            sensor_kind: {
                "channels": f"{meta['band']}?",
                "sensitivity": meta["sensitivity"],
                "input_units": "M/S",
                "output_units": "COUNTS",
                "frequency_hz": args.geophone_hz,
            }
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", required=True, type=Path, help="directory of miniSEED files (searched recursively)")
    p.add_argument("--station-list", required=True, type=Path)
    p.add_argument("--deployment", required=True, help="tag stored on each station entry, e.g. ElPaso2021")
    p.add_argument("--model", default="node", help="instrument model shown on the dashboard")
    p.add_argument("--geophone-hz", type=float, default=None,
                   help="node corner frequency for a geophone response; omit for a flat velocity response")
    p.add_argument("--sensor-kind", default="node", help="instrument block key (node|geophone|seismometer)")
    p.add_argument("--only-stations", action="store_true", help="metadata + stations.json only, no waveforms")
    p.add_argument("--only-waveforms", action="store_true")
    p.add_argument("--target-rate", type=float, default=100.0,
                   help="decimate to this rate (integer factor, anti-aliased); 0 = keep as is")
    p.add_argument("--workers", type=int, default=6, help="parallel stations (per-station layout only)")
    p.add_argument("--raw-dir", type=Path, default=RAW_DIR, help=argparse.SUPPRESS)
    p.add_argument("--meta-dir", type=Path, default=META_DIR, help=argparse.SUPPRESS)
    p.add_argument("--stations-file", type=Path, default=STATIONS_FILE, help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not args.target_rate:
        args.target_rate = None

    log = lambda m: print(m, flush=True)  # noqa: E731
    meta = read_station_list(args.station_list)
    files = sorted(fp for pat in ("*.mseed", "*.SAC", "*.sac") for fp in args.data.rglob(pat))
    if not files:
        raise SystemExit(f"no .mseed/.SAC files under {args.data}")
    log(f"{len(meta)} stations in list, {len(files)} data files")

    # Which stations/channels/time span are actually in the data (headers only)?
    present: dict[str, set[str]] = defaultdict(set)
    files_by_station: dict[str, list[Path]] = defaultdict(list)
    t_first = t_last = None
    rate = None
    per_station_layout = True
    for fp in files:
        keys = set()
        for tr in read(str(fp), headonly=True):
            key = f"{tr.stats.network}.{tr.stats.station}"
            keys.add(key)
            present[key].add(tr.stats.channel)
            t_first = tr.stats.starttime if t_first is None else min(t_first, tr.stats.starttime)
            t_last = tr.stats.endtime if t_last is None else max(t_last, tr.stats.endtime)
            rate = tr.stats.sampling_rate
        if len(keys) > 1:
            per_station_layout = False
        for key in keys:
            files_by_station[key].append(fp)
    log(f"layout: {'per-station files' if per_station_layout else 'multi-station files (hourly)'}"
        + (f"; decimating {rate} -> {args.target_rate} Hz" if args.target_rate and abs(rate - args.target_rate) > 0.01 else ""))
    start_day, end_day = t_first.date, t_last.date
    wanted = {k for k in present if k in meta}
    unknown = sorted(set(present) - set(meta))
    silent = sorted(set(meta) - set(present))
    log(f"data span {t_first} -> {t_last} ({start_day} .. {end_day}), rate {rate}")
    log(f"stations with data: {len(wanted)}; in list but no data: {silent or 'none'}; "
        f"in data but not in list (skipped): {unknown or 'none'}")

    if not args.only_waveforms:
        # metadata
        args.meta_dir.mkdir(parents=True, exist_ok=True)
        for key in sorted(wanted):
            m = meta[key]
            chans = sorted(present[key])
            inv = build_inventory(m, chans, UTCDateTime(start_day), UTCDateTime(end_day) + 86400,
                                  args.geophone_hz, float(args.target_rate or round(rate)))
            xml = args.meta_dir / f"{m['network']}.{m['station']}.xml"
            if args.dry_run:
                continue
            inv.write(str(xml), format="STATIONXML")
        log(f"StationXML: {len(wanted)} files in {args.meta_dir} ({'flat' if not args.geophone_hz else f'{args.geophone_hz} Hz geophone'} response)")

        # stations.json
        stations = json.loads(args.stations_file.read_text())
        existing = {(s["network"], s["station"]) for s in stations}
        new_entries = [station_entry(meta[k], args, start_day, end_day, args.sensor_kind)
                       for k in sorted(wanted) if (meta[k]["network"], meta[k]["station"]) not in existing]
        try:
            warnings = validate_stations(stations + new_entries)
        except StationValidationError as exc:
            raise SystemExit(f"stations.json would be invalid: {exc}") from None
        for w in warnings:
            log(f"  warning: {w}")
        if args.dry_run:
            log(f"[dry-run] would add {len(new_entries)} station(s) to stations.json")
        else:
            atomic_write_text(json.dumps(stations + new_entries, indent=2) + "\n", args.stations_file)
            log(f"stations.json: +{len(new_entries)} station(s), {len(stations) + len(new_entries)} total")

    if not args.only_stations and per_station_layout and not args.dry_run:
        todo = {k: files_by_station[k] for k in sorted(wanted)}
        log(f"importing {len(todo)} stations with {args.workers} worker(s) ...")
        done = 0
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(import_station_files, k, v, args.target_rate, args.raw_dir): k for k, v in todo.items()}
            for fut in as_completed(futs):
                st = fut.result()
                done += 1
                log(f"[{done}/{len(todo)}] {st['station']}: {st['files']} files, {st['pieces']} day-pieces, "
                    f"{st['duplicates']} duplicate window(s) skipped, channels {sorted(st['channels'])}, "
                    f"{'int32' if st['integral'] else 'float32'}")
    elif not args.only_stations:
        stats = import_waveforms(files, wanted, args.dry_run, log, target_rate=args.target_rate, raw_dir=args.raw_dir)
        log(f"waveforms: {stats['files']} input files, {stats['traces']} traces, "
            f"{stats['days']} day(s), {stats['written']} day-files written, "
            f"channels {sorted(stats['channels'])}, rate {stats['rate']}")

    log("\nNext: python scripts/nlloc_build_grids.py   (then steps 2-5 for "
        f"--start {start_day} --end {end_day + __import__('datetime').timedelta(days=1)}; see RUNBOOK.md)")


if __name__ == "__main__":
    main()
