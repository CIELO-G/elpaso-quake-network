#!/usr/bin/env python
"""Add or retire a station in stations.json, safely (review item B9).

Everything a new station needs, in one command, each step validated:

  add      build the entry (instrument block copied from an existing station
           of the same model), validate it (dates, instrument, channel layout,
           inside the study area), confirm the station exists on its FDSN
           server, write stations.json, rebuild the NLLoc travel-time grids,
           and print what happens next.
  retire   set end_date on an existing station (its history stays usable).

Nothing needs restarting: the dashboard reloads stations.json when it
changes, and the orchestrator and every stage read it at the start of each
run. The NLLoc grid rebuild is the one slow step (a few minutes).

Usage
-----
    python scripts/add_station.py add --network AM --station R1234 \\
        --lat 31.90 --lon -106.45 --elev 1200 --model RS3D --start-date 2026-11-01

    python scripts/add_station.py retire --station R5912 --end-date 2026-02-23

    # Preview only
    python scripts/add_station.py add ... --dry-run

See RUNBOOK.md for the full procedure (backfill, what to check after).
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.atomic_io import atomic_write_text  # noqa: E402
from lib.geometry import study_area  # noqa: E402
from lib.models import StationValidationError, validate_stations  # noqa: E402

STATIONS_FILE = ROOT / "stations.json"

# channels pattern per Raspberry Shake model (lib.models.channel_layout agrees)
MODEL_CHANNELS = {"RS3D": "EH?", "RS4D": "E??", "RS1D": "EHZ", "BROADBAND": "HH?"}


def _load() -> list[dict]:
    return json.loads(STATIONS_FILE.read_text())


def _write(stations: list[dict], dry_run: bool) -> None:
    text = json.dumps(stations, indent=2) + "\n"
    if dry_run:
        print(f"[dry-run] would write {STATIONS_FILE} ({len(stations)} stations)")
        return
    atomic_write_text(text, STATIONS_FILE)
    print(f"Wrote {STATIONS_FILE} ({len(stations)} stations)")


def _template_instrument(stations: list[dict], model: str) -> dict:
    """Copy the instrument block from an existing station of the same model."""
    for s in stations:
        if str(s.get("model", "")).upper() == model:
            return copy.deepcopy(s["instrument"])
    raise SystemExit(
        f"No existing {model} station to copy the instrument block from; "
        "add one by hand to stations.json (see an RS3D entry for the shape)."
    )


def _check_fdsn(entry: dict, timeout: float = 60.0) -> None:
    """One metadata request: does the server know this station and its channels?"""
    from obspy.clients.fdsn import Client

    url = entry.get("fdsnws_url") or (
        "https://data.raspberryshake.org" if entry["network"] == "AM" else "IRIS"
    )
    print(f"Checking {entry['network']}.{entry['station']} on {url} ...")
    client = Client(url, timeout=timeout)
    inv = client.get_stations(
        network=entry["network"], station=entry["station"], level="channel"
    )
    chans = sorted({c.code for n in inv for s in n for c in s})
    if not chans:
        raise SystemExit("  server returned no channels for this station")
    print(f"  found channels: {', '.join(chans)}")
    want = entry["channels"]
    import fnmatch

    if not any(fnmatch.fnmatch(c, want) for c in chans):
        raise SystemExit(f"  none of them match channels={want!r}; check the model")


def _build_grids(dry_run: bool) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / "nlloc_build_grids.py")]
    if dry_run:
        print(f"[dry-run] would run: {' '.join(cmd)}")
        return
    print("Rebuilding NLLoc travel-time grids (a few minutes) ...")
    subprocess.run(cmd, check=True, cwd=ROOT)


def cmd_add(args: argparse.Namespace) -> None:
    stations = _load()
    if any(s["station"] == args.station and s["network"] == args.network for s in stations):
        raise SystemExit(f"{args.network}.{args.station} is already in stations.json")

    model = args.model.upper()
    entry: dict = {
        "network": args.network,
        "station": args.station,
        "location": args.location,
        "channels": args.channels or MODEL_CHANNELS.get(model, "EH?"),
        "latitude": args.lat,
        "longitude": args.lon,
        "elevation_m": args.elev,
        "sample_rate_hz": args.sample_rate,
        "start_date": args.start_date,
        "model": args.model,
        "instrument": _template_instrument(stations, model),
    }
    if args.fdsnws_url:
        entry["fdsnws_url"] = args.fdsnws_url

    # Validate the whole list with the new entry in place (dates, instrument,
    # channel layout, inside the study area) — same checks every stage runs.
    try:
        warnings = validate_stations(stations + [entry])
    except StationValidationError as exc:
        box = study_area()
        raise SystemExit(
            f"Rejected: {exc}\n(study area: lat {box['lat_min']:.2f}..{box['lat_max']:.2f}, "
            f"lon {box['lon_min']:.2f}..{box['lon_max']:.2f})"
        ) from None
    for w in warnings:
        print(f"  warning: {w}")

    print("New entry:")
    print(json.dumps(entry, indent=2))

    if not args.skip_fdsn_check:
        _check_fdsn(entry)

    _write(stations + [entry], args.dry_run)
    if not args.skip_grids:
        _build_grids(args.dry_run)

    print(
        "\nDone. What happens now:\n"
        f"  - ingest picks {entry['network']}.{entry['station']} up from its next run "
        f"(data from {entry['start_date']} onward)\n"
        "  - the dashboard shows it on the next page load (no restart)\n"
        "  - to archive its history before today, stop the pipeline in the dashboard and run:\n"
        f"      python run_pipeline.py --gap-fill --archive-only --start {entry['start_date']}\n"
        "    (~4 min per station-day; then press Start again)\n"
        "  - RUNBOOK.md has the checks to do after the first day runs."
    )


def cmd_retire(args: argparse.Namespace) -> None:
    stations = _load()
    hits = [s for s in stations if s["station"] == args.station]
    if not hits:
        raise SystemExit(f"{args.station} is not in stations.json")
    if len(hits) > 1 and not args.network:
        raise SystemExit(f"{args.station} exists in more than one network; pass --network")
    entry = next(s for s in hits if not args.network or s["network"] == args.network)
    date.fromisoformat(args.end_date)  # format check
    entry["end_date"] = args.end_date
    try:
        validate_stations(stations)
    except StationValidationError as exc:
        raise SystemExit(f"Rejected: {exc}") from None
    print(f"{entry['network']}.{entry['station']}: end_date = {args.end_date}")
    _write(stations, args.dry_run)
    print(
        "\nDone. Ingest stops requesting it, the dark-station alert ignores it, the\n"
        "dashboard shows it as Retired, and its history is still used when an old\n"
        "day is re-run. If it is in 5-catalog/config.yaml triage.core_stations, the\n"
        "catalog step will warn: remove it from that list."
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a station")
    a.add_argument("--network", default="AM")
    a.add_argument("--station", required=True, help="e.g. R1234")
    a.add_argument("--lat", type=float, required=True)
    a.add_argument("--lon", type=float, required=True)
    a.add_argument("--elev", type=float, required=True, help="elevation in metres")
    a.add_argument("--model", required=True, help="RS3D, RS4D, RS1D or broadband")
    a.add_argument("--start-date", required=True, help="first day with data, YYYY-MM-DD")
    a.add_argument("--location", default="00")
    a.add_argument("--channels", default=None, help="override the channel pattern (default from model)")
    a.add_argument("--sample-rate", type=float, default=100.0)
    a.add_argument("--fdsnws-url", default=None, help="only for non-AM networks")
    a.add_argument("--skip-fdsn-check", action="store_true")
    a.add_argument("--skip-grids", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("retire", help="set a station's end_date")
    r.add_argument("--station", required=True)
    r.add_argument("--network", default=None)
    r.add_argument("--end-date", required=True, help="last day with data, YYYY-MM-DD")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_retire)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
