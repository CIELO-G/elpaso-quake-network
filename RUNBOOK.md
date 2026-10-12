# El Paso Monitor — Runbook

Operating procedures for the live system. Design notes are in
`ENGINEERING_REVIEW.md`; the running log is `STATUS.md`. Everything here was
written for the Mac Studio install (2026-10) — paths assume it.

## Where things are

| What | Where |
|---|---|
| Code, catalog, picks, events, logs | `~/Research/Elpaso/elpaso-quake-network` (SSD) |
| Raw and processed waveforms | `/Volumes/VelascoRAID1/Data/ElPaso/elpaso-quake-network/output/` (symlinked as `output/1-raw`, `output/2-processed`) |
| Nightly copy of the SSD part | `/Volumes/VelascoRAID1/Projects/ElPaso` (LaunchAgent `org.elpaso.results-backup`, 03:30 local) |
| The app | `/Applications/El Paso Monitor.app` → dashboard on `http://127.0.0.1:8000`, remote via Tailscale |
| Secrets (dashboard password, alert email) | `deploy/app/local.env` — gitignored, never copy it anywhere |
| Conda envs | `elpaso-quake` (pipeline + dashboard), `phasenet` (PhaseNet, TF 2.11), `phaseno` |

The app supervises the pipeline: opening it starts the web server and the
orchestrator in continuous mode; closing the window stops both cleanly.
Only one machine may run the pipeline.

## Daily rhythm

A UTC day is fetched and processed once it has ended plus a 2 h buffer, so
the run starts at **02:00 UTC (20:00 local)** and takes about an hour. Before
each day the orchestrator re-requests any still-short hours of the last 7
days (station outages) and only re-processes a day when something new
arrived. All requests to the Raspberry Shake server are serial, 3 s apart;
the server refuses parallel requests.

Health at a glance: `curl -s http://127.0.0.1:8000/api/health` — pipeline
status, catalog size, disk, `alerting`, `nlloc_missing_grids`.
`output/pipeline_state.json` lists skipped days, partial days, dark stations.

Alerts go by email (see `local.env`) when a day fails all retries, a station
is dark for 3 days, a partial day ages out, or the watchdog gives up.

## Adding a station

You need: network and code (`AM.R1234`), model (RS3D, RS4D, RS1D), lat/lon,
elevation (m), the first day it recorded. Then, from the repo root in the
`elpaso-quake` env:

```
python scripts/add_station.py add --network AM --station R1234 \
    --lat 31.90 --lon -106.45 --elev 1200 --model RS3D --start-date 2026-11-01
```

The script:

1. builds the entry (instrument block copied from an existing station of
   the same model) and validates it: dates, instrument, channel layout,
   **inside the study area** (`lib/constants.py`, ±1.0° around the network —
   a station outside it is rejected, see "Changing the study area");
2. asks the FDSN server once to confirm the station and its channels exist;
3. writes `stations.json`;
4. rebuilds the NLLoc travel-time grids (a few minutes, all stations).

Nothing needs restarting: the dashboard reloads `stations.json` when it
changes, and the orchestrator and every stage read it at the start of each
run. The station shows on the map immediately and is fetched from the next
daily run.

**History before today.** The daily run only fetches the current day. To
archive what the station recorded earlier:

1. press **Stop** in the dashboard (so there are no parallel requests);
2. `python run_pipeline.py --gap-fill --archive-only --start 2026-11-01`
   — ingest + processing only, ~4 min per station-day;
3. press **Start**. The orchestrator catches up on any daily run it missed.

`--archive-only` deliberately does **not** re-run detection and association
on days that already have events: re-association renumbers event IDs and
would orphan reviews. The station contributes to the catalog from the day
it was added; its earlier data is archived for research use. (Phase 3,
time-based event IDs, lifts this restriction.)

**After the first daily run**, check:

- `logs/ingest.log`: the station's chunks fetched, coverage near 100 %;
- `output/2-processed/<year>/<jday>/` has its files;
- `/api/health` → `nlloc_missing_grids` is empty;
- the station's picks appear in `output/3-picks/...` (a quiet station
  gives tens per day; thousands means a noise problem — see RADA1 in
  `STATUS.md`, 2026-10-12).

Triage: real events are built on the four "core" stations in
`5-catalog/config.yaml`. An event picked by 5+ distinct stations bypasses
that rule, so new stations can carry events into the review queue without
retuning. Revisit the core list after a few months of data from the new
stations.

## Retiring a station

```
python scripts/add_station.py retire --station R5912 --end-date 2026-02-23
```

Ingest stops requesting it, the dark-station alert ignores it, the map
shows it as Retired, and its history is still used when an old day is
re-run. If it is in `triage.core_stations`, remove it there (the catalog
step warns every run until you do).

## Changing the study area

One place: `lib/constants.py` (`NETWORK_CENTER_LAT/LON`,
`NETWORK_HALF_WIDTH_DEG`). Everything derives from it. After a change:

```
python scripts/nlloc_build_grids.py --force     # grids depend on the extent
```

then restart the app. Association, the map rectangle and the station
validation follow automatically.

## Restarting the app

Close the window (or quit the app) — the pipeline is stopped gracefully
first. Reopen from `/Applications`. Required after changing Python code in
`dashboard/`, `run_pipeline.py` or `lib/constants.py`; **not** required for
stage scripts (`1-ingestion/…` to `5-catalog/…`), `stations.json`, or
static dashboard files, which are picked up on the next run or page load.

Rebuild the app only when `deploy/app/local.env` changes:
`./deploy/app/build_app.sh`, then reopen it.

## When something goes wrong

| Symptom | Where to look | What it usually is |
|---|---|---|
| Day listed in `days_skipped` | `logs/pipeline_stdout.log`, search the date | A stage exited 1; the orchestrator retries 3× per cycle and emails when it gives up |
| Day stuck in `days_partial` | `logs/ingest.log` "short chunk" | A station delivering less than 98 % of an hour; re-requested for 7 days, then left as is |
| Station `error` on the map / dark alert | `output/1-raw/<year>/<jday>/` | No files for 3 days: Shake offline, or its uplink |
| "too many requests" in `ingest.log` | `1-ingestion/config.yaml` | Raise `station_delay_seconds` back to 5 |
| `Steim2 encoding failed` warning | `logs/ingest.log` | A corrupt record from the Shake; handled (INT32 fallback), no action |
| `impossible sample(s) masked` warning | `logs/process.log`, `detect.log` | Same corrupt record reaching the filters; handled |
| Thousands of picks/day from one station | `output/3-picks` | Noise or data loss at the station (see RADA1 check, `STATUS.md`) |
| Data volume not mounted | Launcher alert | Mount the RAID, reopen the app |

Manual re-run of one day, all stages, with the app running (orchestrator
idle between 03:00 and 02:00 UTC):

```
for s in 1-ingestion/ingest.py 2-processing/process.py 3-detection/detect.py 4-association/associate.py; do
  python -u $s --config ${s%/*}/config.yaml --start 2026-10-06 --end 2026-10-07 || break
done
python -u 5-catalog/catalog.py --config 5-catalog/config.yaml
```

Never run `scripts/prune_raw.py` on this machine (raw lives on the RAID; the
script refuses, by design).

## Tests

```
/opt/anaconda3/envs/elpaso-quake/bin/python -m pytest -q -p no:cacheprovider -o addopts=""
```

Runs in under a minute; the NLLoc tests skip if the binary or grids are
missing. Run it before merging anything to `main`.
