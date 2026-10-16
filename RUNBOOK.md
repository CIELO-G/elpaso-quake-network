# El Paso Monitor — Runbook

Operating procedures for the live system. Design notes are in
`docs/ENGINEERING_REVIEW.md`; the running log is `STATUS.md`. Everything here was
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

## Regional-earthquake tags

Delaware Basin earthquakes reach the network as coherent wave trains that
GaMMA can associate into a "local" event 5–90 s after the real one. The
catalog step auto-rejects events 5–90 s after a ComCat M ≥ 3.0 at 150–350 km
(`triage.regional` in `5-catalog/config.yaml`; reason "N s after regional
M…, <place> (<ComCat id>)"). Lowering `min_magnitude` to 2.0 catches a few
more false events (11 instead of 8 of the 50 rejected), still no confirmed
ones. Like every triage label it only orders the queue; review_status is
never changed, and the "auto-rejected" list in the dashboard still shows them.

ComCat is cached in `output/5-catalog/comcat_regional.csv` (+ `.json` with
the span it covers); each run re-fetches the last 7 days. If ComCat is
unreachable, `logs/catalog.log` says "ComCat refresh failed" and the cache is
used. Events past the cached span are not tagged until a later run succeeds.
After changing the thresholds, relabel the whole catalog:

```
python 5-catalog/catalog.py --config 5-catalog/config.yaml --retriage
```

Deleting both cache files forces a full re-download (about 5 s).

## PhaseNO second detector

PhaseNet stays the primary picker. The optional `phaseno` step (between
associate and catalog) picks the same day with PhaseNO on the GPU, associates
those picks with the production GaMMA settings, and hands the catalog only the
events PhaseNet **missed but saw**: no PhaseNet event within 10 s, and at
least 3 PhaseNO picks matching PhaseNet picks (same station and phase, ±1 s).
They get ids `poYYYYMMDD-NNNN`, `detector = PhaseNO`, the matching-pick count
in `corroborating_picks`, a **PhaseNO** tag in the dashboard, and the same
triage as everything else. Evaluated in
`research/phaseno-eval/FINDINGS.md` (Round 2): 15/17 confirmed events on 20
full days against 12/17 for PhaseNet alone, about 0.2 more queue items a day.

- **On/off:** `enabled:` in `3-detection/phaseno/config.yaml`, read before
  every day (no restart). Off, the step shows as skipped and the catalog is
  PhaseNet's alone.
- **Cost:** 4–7 min of GPU time per day (`logs/phaseno.log`; its association
  logs to `logs/associate_phaseno.log`).
- **Outputs:** `output/phaseno/{3-picks,4-events,4-additions}/`. PhaseNet's
  files are only read.
- **Failure** never fails the day: the step is marked failed, an alert is
  sent with the commands to redo it, and the catalog is built without it.
  When redone by hand, the next catalog run adds that day's PhaseNO events
  (each day's additions are ingested once, separately from PhaseNet's).
- **Backfill** (with the orchestrator idle, like any manual run; the step
  skips days that already have PhaseNO picks):

  ```
  python 3-detection/phaseno/step.py --config 3-detection/phaseno/config.yaml \
      --start 2026-01-01 --end 2026-10-01
  python 5-catalog/catalog.py --config 5-catalog/config.yaml
  ```

## Retiring a station

```
python scripts/add_station.py retire --station R5912 --end-date 2026-02-23
```

Ingest stops requesting it, the dark-station alert ignores it, the map
shows it as Retired, and its history is still used when an old day is
re-run. If it is in `triage.core_stations`, remove it there (the catalog
step warns every run until you do).

## Importing a temporary deployment

A campaign (nodes, a few weeks) arrives as local miniSEED with a station
list, not from FDSN. Keep the original files on the RAID
(`/Volumes/VelascoRAID1/Data/ElPaso/deployments/<name>/`) and import from
there:

```
python scripts/import_deployment.py \
    --data /Volumes/VelascoRAID1/Data/ElPaso/deployments/ElPaso2021_nodal/sac \
    --station-list /Volumes/VelascoRAID1/Data/ElPaso/deployments/ElPaso2021_nodal/Station_list.csv \
    --deployment ElPaso2021 --model "Fairfield ZLand 3C node" --geophone-hz 5 --workers 6
python scripts/nlloc_build_grids.py
```

The tool writes per-station day files to the raw archive (float32, clock
snapped to 100 Hz), StationXML with the list's sensitivity (flat velocity
response unless `--geophone-hz`), and `stations.json` entries with both
`start_date` and `end_date` — the stations are born retired, so the daily
pipeline never requests them and the map hides them behind the
**Retired** button. Then run steps 2–5 for the campaign dates (the ingest
step is not involved):

```
for s in 2-processing/process.py 3-detection/detect.py 4-association/associate.py; do
  python -u $s --config ${s%/*}/config.yaml --start 2021-06-05 --end 2021-06-16 || break   # --end is exclusive
done
python -u 5-catalog/catalog.py --config 5-catalog/config.yaml
```

Do this while the orchestrator is idle (between 03:00 and 02:00 UTC) or
with the pipeline stopped: the catalog step takes the same lock the daily
run does, so they cannot corrupt each other, but PhaseNet on 49 stations
and the daily run would compete for CPU.

Expect the campaign's events to show up with the pipeline's own
thresholds and GaMMA settings, which were tuned for the sparse Shake
network; a dense array may deserve its own `4-association/config.yaml`
values. Review before trusting.

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
