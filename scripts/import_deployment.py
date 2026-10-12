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


def normalise_trace(tr, rate_tol: float = NOMINAL_RATE_TOL) -> None:
    """Snap a near-nominal node clock to the nominal rate; store float32."""
    nominal = round(tr.stats.sampling_rate)
    if abs(tr.stats.sampling_rate - nominal) <= rate_tol:
        tr.stats.sampling_rate = float(nominal)
    if tr.data.dtype != np.float32:
        tr.data = tr.data.astype(np.float32)


def day_key(t: UTCDateTime) -> tuple[int, int]:
    return t.year, t.julday


def import_waveforms(files: list[Path], wanted: set[str], dry_run: bool, log) -> dict:
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
        day_dir = RAW_DIR / str(year) / f"{jday:03d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        for tid, s in by_id.items():
            net, sta, loc, cha = tid.split(".")
            fpath = day_dir / f"{net}.{sta}.{loc}.{cha}.{year}.{jday:03d}.mseed"
            if fpath.exists():
                s += read(str(fpath))
            s.merge(method=1)
            s = s.split()
            atomic_write_stream(s, fpath, format="MSEED", encoding="FLOAT32")
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
            normalise_trace(tr)
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
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    log = lambda m: print(m, flush=True)  # noqa: E731
    meta = read_station_list(args.station_list)
    files = sorted(fp for fp in args.data.rglob("*.mseed"))
    if not files:
        raise SystemExit(f"no .mseed files under {args.data}")
    log(f"{len(meta)} stations in list, {len(files)} data files")

    # Which stations/channels/time span are actually in the data (headers only)?
    present: dict[str, set[str]] = defaultdict(set)
    t_first = t_last = None
    rate = None
    for fp in files:
        for tr in read(str(fp), headonly=True):
            present[f"{tr.stats.network}.{tr.stats.station}"].add(tr.stats.channel)
            t_first = tr.stats.starttime if t_first is None else min(t_first, tr.stats.starttime)
            t_last = tr.stats.endtime if t_last is None else max(t_last, tr.stats.endtime)
            rate = tr.stats.sampling_rate
    start_day, end_day = t_first.date, t_last.date
    wanted = {k for k in present if k in meta}
    unknown = sorted(set(present) - set(meta))
    silent = sorted(set(meta) - set(present))
    log(f"data span {t_first} -> {t_last} ({start_day} .. {end_day}), rate {rate}")
    log(f"stations with data: {len(wanted)}; in list but no data: {silent or 'none'}; "
        f"in data but not in list (skipped): {unknown or 'none'}")

    if not args.only_waveforms:
        # metadata
        META_DIR.mkdir(parents=True, exist_ok=True)
        for key in sorted(wanted):
            m = meta[key]
            chans = sorted(present[key])
            inv = build_inventory(m, chans, UTCDateTime(start_day), UTCDateTime(end_day) + 86400,
                                  args.geophone_hz, float(round(rate)))
            xml = META_DIR / f"{m['network']}.{m['station']}.xml"
            if args.dry_run:
                continue
            inv.write(str(xml), format="STATIONXML")
        log(f"StationXML: {len(wanted)} files in {META_DIR} ({'flat' if not args.geophone_hz else f'{args.geophone_hz} Hz geophone'} response)")

        # stations.json
        stations = json.loads(STATIONS_FILE.read_text())
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
            atomic_write_text(json.dumps(stations + new_entries, indent=2) + "\n", STATIONS_FILE)
            log(f"stations.json: +{len(new_entries)} station(s), {len(stations) + len(new_entries)} total")

    if not args.only_stations:
        stats = import_waveforms(files, wanted, args.dry_run, log)
        log(f"waveforms: {stats['files']} input files, {stats['traces']} traces, "
            f"{stats['days']} day(s), {stats['written']} day-files written, "
            f"channels {sorted(stats['channels'])}, rate {stats['rate']}")

    log("\nNext: python scripts/nlloc_build_grids.py   (then steps 2-5 for "
        f"--start {start_day} --end {end_day + __import__('datetime').timedelta(days=1)}; see RUNBOOK.md)")


if __name__ == "__main__":
    main()
