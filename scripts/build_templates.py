#!/usr/bin/env python3
"""Build EQcorrscan template library from confirmed quarry blasts.

Reads confirmed ``event_type=quarry_blast`` events from the catalog, fetches
the processed waveforms around each event, extracts a time-windowed template
per station+channel, and saves the result as a single ``Tribe`` pickle for
fast loading by the detection runner.

Usage
-----
    python scripts/build_templates.py
    python scripts/build_templates.py --event-type quarry_blast --before 1.0 --after 5.0
    python scripts/build_templates.py --filter 2 20 --samp-rate 50

Outputs
-------
    output/templates/blast_templates.tgz   (EQcorrscan Tribe archive)
    output/templates/manifest.json         (template ID → event_id, source)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from obspy import UTCDateTime, read

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


CATALOG_PATH = ROOT / "output" / "5-catalog" / "catalog.csv"
ASSIGNMENTS_PATH = ROOT / "output" / "5-catalog" / "assignments.csv"
PROCESSED_DIR = ROOT / "output" / "2-processed"
TEMPLATE_DIR = ROOT / "output" / "templates"


def load_blast_events(event_type: str) -> pd.DataFrame:
    """Return confirmed events of a given type from the catalog."""
    df = pd.read_csv(CATALOG_PATH, dtype={"event_id": str})
    mask = (df["review_status"].fillna("") == "confirmed") & (
        df["event_type"].fillna("") == event_type
    )
    return df[mask].copy()


def load_event_picks(event_ids: set[str]) -> dict[str, list[dict]]:
    """Index assignments by event_id for fast lookup."""
    df = pd.read_csv(ASSIGNMENTS_PATH, dtype={"event_id": str})
    df = df[df["event_id"].isin(event_ids)]
    out: dict[str, list[dict]] = defaultdict(list)
    for row in df.to_dict("records"):
        out[row["event_id"]].append(row)
    return out


def fetch_event_stream(event_time: UTCDateTime, before_s: float, after_s: float):
    """Read every processed mseed file overlapping the event window.

    Uses a generous ±30 s margin around the event so every pick has enough
    surrounding data for EQcorrscan to extract a full-length template, even
    if pick times are 5-10 s offset from event_time.
    """
    from obspy import Stream

    stream = Stream()
    margin = max(30.0, (before_s + after_s) * 2)
    start = event_time - margin
    end = event_time + margin
    # Walk processed files for any day touched by the window
    day1 = start.datetime.timetuple()
    day2 = end.datetime.timetuple()
    days = {(day1.tm_year, day1.tm_yday), (day2.tm_year, day2.tm_yday)}
    for year, jday in days:
        day_dir = PROCESSED_DIR / f"{year:04d}" / f"{jday:03d}"
        if not day_dir.exists():
            continue
        for mseed in day_dir.glob("*.mseed"):
            try:
                stream += read(str(mseed))
            except Exception:
                continue
    if not len(stream):
        return stream
    stream.trim(starttime=start, endtime=end)
    stream.merge(fill_value=0)
    return stream


def build_single_event_tribe(
    event_id: str,
    event_time: UTCDateTime,
    picks: list[dict],
    before_s: float,
    after_s: float,
    filt_low: float,
    filt_high: float,
    samp_rate: float,
    logger: logging.Logger,
):
    """Build a single-event Tribe using Tribe.construct(from_meta_file).

    Returns the single Template inside the Tribe (or None on failure).
    Renames it to ``event_id`` for clean lookup.
    """
    from eqcorrscan.core.match_filter import Tribe
    from obspy import UTCDateTime as UTC
    from obspy.core.event import Catalog as ObsCatalog
    from obspy.core.event import Event, Origin, Pick, WaveformStreamID

    stream = fetch_event_stream(event_time, before_s, after_s)
    if not len(stream):
        logger.warning("  No waveforms found for %s", event_id)
        return None

    obs_event = Event()
    obs_event.origins.append(Origin(time=event_time))
    obs_event.preferred_origin_id = obs_event.origins[0].resource_id
    for p in picks:
        pick_time_str = p.get("time", "")
        phase = p.get("phase", "")
        if not pick_time_str or not phase:
            continue
        try:
            pick_time = UTC(pick_time_str)
        except Exception:
            continue
        wf_id = WaveformStreamID(
            network_code=p.get("network", ""),
            station_code=p.get("station", ""),
            location_code=p.get("location") or "",
            channel_code=p.get("channel", ""),
        )
        obs_event.picks.append(Pick(time=pick_time, phase_hint=phase, waveform_id=wf_id))

    if not obs_event.picks:
        logger.warning("  %s has no picks — skipping (need picks for alignment)", event_id)
        return None

    cat = ObsCatalog(events=[obs_event])
    try:
        tribe = Tribe().construct(
            method="from_meta_file",
            meta_file=cat,
            st=stream,
            lowcut=filt_low,
            highcut=filt_high,
            samp_rate=samp_rate,
            filt_order=4,
            length=before_s + after_s,
            prepick=before_s,
            swin="all",
            process_len=max(int(before_s + after_s + 10), 60),  # short window per event
        )
    except Exception as e:
        logger.warning("  Tribe construction failed for %s: %s", event_id, e)
        return None

    if not tribe.templates:
        logger.warning("  No template produced for %s", event_id)
        return None

    t = tribe.templates[0]
    t.name = event_id

    # EQcorrscan's match_filter requires every trace in a template to be the
    # same length. Drop any short traces (usually caused by pick timing near
    # the edge of available data); keep templates that still have ≥4 traces.
    expected_len = int(round((before_s + after_s) * samp_rate))
    keep = [tr for tr in t.st if len(tr.data) == expected_len]
    dropped = len(t.st) - len(keep)
    if dropped:
        logger.info("  Dropped %d short trace(s) (length != %d samples)", dropped, expected_len)
    if len(keep) < 4:
        logger.warning(
            "  %s ended up with only %d full-length traces — skipping", event_id, len(keep)
        )
        return None
    t.st.traces = keep
    return t


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-type", default="quarry_blast", help="Filter by event_type (default: quarry_blast)"
    )
    parser.add_argument(
        "--before", type=float, default=1.0, help="Seconds before pick to include in template"
    )
    parser.add_argument(
        "--after", type=float, default=5.0, help="Seconds after pick to include in template"
    )
    parser.add_argument(
        "--filter",
        type=float,
        nargs=2,
        default=[2.0, 20.0],
        metavar=("LOW", "HIGH"),
        help="Bandpass corners in Hz (default 2 20)",
    )
    parser.add_argument(
        "--samp-rate",
        type=float,
        default=50.0,
        help="Resample to this rate before template-building",
    )
    parser.add_argument("--out-dir", type=Path, default=TEMPLATE_DIR)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("templates")

    from eqcorrscan.core.match_filter import Tribe

    events = load_blast_events(args.event_type)
    if events.empty:
        logger.error("No confirmed events of type '%s' in catalog", args.event_type)
        sys.exit(1)
    logger.info("Found %d confirmed %s events", len(events), args.event_type)

    picks_by_event = load_event_picks(set(events["event_id"]))
    logger.info(
        "Loaded picks for %d of %d events",
        sum(1 for eid in events["event_id"] if eid in picks_by_event),
        len(events),
    )

    templates = []
    skipped = 0
    for _, row in events.iterrows():
        eid = row["event_id"]
        try:
            t0 = UTCDateTime(row["time"])
        except Exception:
            logger.warning("Bad time for %s — skip", eid)
            skipped += 1
            continue
        logger.info("Building template %s @ %s", eid, t0)
        tmpl = build_single_event_tribe(
            event_id=eid,
            event_time=t0,
            picks=picks_by_event.get(eid, []),
            before_s=args.before,
            after_s=args.after,
            filt_low=args.filter[0],
            filt_high=args.filter[1],
            samp_rate=args.samp_rate,
            logger=logger,
        )
        if tmpl is None:
            skipped += 1
            continue
        templates.append(tmpl)
        logger.info("  → %d traces in template", len(tmpl.st))

    if not templates:
        logger.error("No usable templates produced — aborting")
        sys.exit(1)

    tribe = Tribe(templates=templates)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = args.out_dir / "blast_templates.tgz"
    tribe.write(str(archive_path.with_suffix("")))  # EQcorrscan adds .tgz
    logger.info("Wrote %d templates → %s", len(tribe), archive_path)

    # Write a manifest for fast lookup by external tools
    manifest = {
        "n_templates": len(tribe),
        "event_type_filter": args.event_type,
        "template_params": {
            "before_s": args.before,
            "after_s": args.after,
            "filter_low": args.filter[0],
            "filter_high": args.filter[1],
            "samp_rate": args.samp_rate,
        },
        "templates": [
            {
                "id": t.name,
                "n_traces": len(t.st),
                "stations": sorted({tr.stats.station for tr in t.st}),
                "channels": sorted({f"{tr.stats.station}.{tr.stats.channel}" for tr in t.st}),
            }
            for t in templates
        ],
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    logger.info(
        "Built %d templates, skipped %d events (no picks/waveforms/etc).",
        len(templates),
        skipped,
    )


if __name__ == "__main__":
    main()
