# Engineering review — elpaso-quake-network (2026-10-02)

Read-only review of the whole repository with one question in mind: can this run
unattended in a research lab for years, be handed to other lab members, and grow
from 14 to ~24 stations? Four focused passes (pipeline core, dashboard, operations,
station scaling) plus spot verification of every high-severity claim. Nothing was
changed. File:line references are to the working tree on the Mac Studio.

**Verdict.** The science core is careful and the code is readable, well commented,
and tested where it matters most (`lib/`, catalog merge, auth). The weaknesses are
operational: several failure paths are silent, the human review record can be lost
by a race, alerting is wired but never configured, and ingestion is serial by
design. None of this needs a rewrite. It needs a short hardening phase before the
new stations arrive, then a modest restructuring so the next maintainer can work
on it safely.

## A. Fix before anything else (data loss and silent failure)

| # | Problem | Where | Fix |
|---|---|---|---|
| A1 | **Reviews can be overwritten.** The catalog step does an unlocked read-modify-write of `catalog.csv`; the dashboard's lock is an `asyncio.Lock`, in-process only. A review saved while step 5 runs is lost. `reviews.jsonl` is appended only on `--rebuild`, so dashboard saves since 5 July exist only in `catalog.csv`. | `5-catalog/catalog.py:472-579`, `dashboard/routes/review.py:36,278,368` | Cross-process `fcntl.flock` on a sentinel used by both writers; append every dashboard save to `reviews.jsonl`. |
| A2 | **Ingest never fails.** Exit code is 0 even when every station fails, so the orchestrator marks the day complete and never retries or alerts. | `1-ingestion/ingest.py:736-749` | `sys.exit(1)` when `failed > 0`, as `detect.py:496-501` already does. |
| A3 | **Association failure marks the day done.** On a GaMMA exception it writes empty events/assignments CSVs and exits 0. | `4-association/associate.py:692-698` | Leave outputs absent and exit non-zero. |
| A4 | **A crash mid-day skips that day forever.** Resume point is the newest `2-processed` day + 1; processing creates that directory before detect/associate/catalog have run. Gap-fill only checks raw/processed presence. | `run_pipeline.py:436-460, 207-250` | Resume from the last day with `4-events/*.events.csv`; gap-fill checks picks/events too. |
| A5 | **Skipped days vanish on restart.** `days_skipped` lives only in memory. | `run_pipeline.py:514,709` | Persist to the status file and reload at start. |
| A6 | **Partial hours are permanent.** A chunk is `success` whatever fraction came back; Raspberry Shakes upload late after outages, and that data is never fetched. | `1-ingestion/ingest.py:484-499`, `lib/db.py:48-65` | Store coverage per chunk; re-fetch chunks below ~99% until a configurable age. |
| A7 | **Alerts never fire from the app.** `_send_alert` and the webhook read `ALERT_*` env vars; the launcher exports only dashboard vars. Nothing notifies when a station goes dark for weeks, or when the watchdog gives up. | `deploy/app/build_app.sh:101-106`, `run_pipeline.py:170-194`, `lib/monitoring.py:357-413`, `dashboard/appmode.py:146-150` | Put `ALERT_*` in `local.env` and the launcher; add a per-station coverage check run daily; alert on watchdog exhaustion; show alert status in `/api/health`. |
| A8 | **`scripts/prune_raw.py` is now dangerous.** It verifies against `/Volumes/Marc` and deletes from `output/1-raw`, which is the RAID. | `scripts/prune_raw.py:27` | Refuse to run when `output/1-raw` resolves outside the repo, or delete the script. |
| A9 | **Stop forgets the pipeline process.** SIGTERM is sent, then the handle and PID file are dropped without `wait()`; the app can exit and relaunch a second pipeline while the first is still finishing. | `dashboard/routes/pipeline.py:376-381`, `appmode.py:101-108` | Keep the handle until `poll()` returns, SIGKILL after a timeout. |
| A10 | **Stage outputs are not atomic.** Raw day files, processed files, picks and events are written in place; a kill mid-write leaves a truncated file that the "already done" checks accept. `lib/atomic_io.py` exists but only the catalog uses it. | `ingest.py:370`, `process.py:238`, `detect.py:363-367`, `associate.py:578-594` | Route all stage outputs through tmp + `os.replace`. |
| A11 | **Zero-fill in raw storage.** Ingest merges with `fill_value=0`, so gaps become zeros on disk; the later `interpolate` fills never trigger and zeros reach PhaseNet and ML. | `ingest.py:369`, `process.py:138`, `detect.py:172` | Store gaps as gaps; fill only in the consumer. |
| A13 | **One corrupt chunk aborts the whole ingest run.** Only `FDSNException`/`OSError` are caught per chunk; a libmseed packing error (Steim2 cannot encode a corrupt Raspberry Shake record) escapes the worker thread and the run exits 1 for every station, on every retry (2026-10-06 incident). | `ingest.py` `process_station`, `save_waveforms` | Catch any exception per chunk and record it as failed; fall back to a lossless encoding when Steim2 cannot represent the data. *Added 2026-10-12, done.* |
| A14 | **Corrupt samples reach the filters and PhaseNet.** Processing and detection both consume raw counts as-is; a corrupt Steim2 record decodes to samples ~1e8 counts, and the bandpass turns each into a wavelet. | `process.py` `process_file`, `detect.py` `merge_raw_data` | Shared guard `lib/waveform_qc.py`: samples beyond the 24-bit ADC range become gaps and are interpolated like any other gap. *Added 2026-10-12, done.* |
| A12 | **NaN amplitude reaches ML.** `amp_vel <= 0` is False for NaN. | `associate.py:463-475`, `lib/magnitude.py:96` | Explicit NaN check. |

## B. Before adding the 10 stations

| # | Problem | Where | Fix |
|---|---|---|---|
| B1 | *(Done 2026-10-12: day-file written once per station-day, tracker rows only after the write; no pause after an empty reply; delay 5→3 s per Marc. Requests stay serial. Live check: output identical, 8.4 s/chunk vs 10.)* **Ingest is serial: ~90 min/day at 24 stations, ~10 days to backfill a year for 10 stations.** `max_workers: 1`, 1-hour chunks, 5 s sleep per chunk (46 min of pure sleep at 24 stations). The day file is re-read and rewritten after every chunk. | `1-ingestion/config.yaml:22,34,38`, `ingest.py:362-370,446-550` | The Raspberry Shake server rejects concurrent requests ("too many requests" seen in practice), so RS stays serial. Gains available: run IRIS/other hosts in a separate worker, write the day file once instead of 24 times, and keep new stations' `start_date` honest so no long backfill is attempted. Accept ~90 min/day at 24 stations; continuous mode tolerates it. |
| B2 | *(Done 2026-10-12: `start_date` required, `end_date` for retirement, `instrument` and channel layout validated at load via `lib/models.velocity_sensor` / `channel_layout`; detection and the orchestrator use the validated loader.)* **Two KeyErrors waiting for the first new entry.** `start_date` missing kills `--gap-fill` for the whole network; `instrument.geophone` missing crashes detection for every day. Neither is validated. | `run_pipeline.py:204`, `3-detection/detect.py:116,132`, `lib/models.py:86-146` | Validate `start_date`, `model`, `instrument` in `validate_station`; derive channels from the `channels` field, not from string sniffing. |
| B3 | *(Done 2026-10-12: every station's P and S grid checked; missing ones excluded and listed in `NLLocLocator.missing_grids` and `/api/health.nlloc_missing_grids`; `require_all_grids=True` for scripts.)* **NLLoc grids are checked for `stations[0]` only.** New stations silently contribute nothing to relocation until someone remembers `scripts/nlloc_build_grids.py`. | `lib/location/nlloc.py:107-115` | Check every station's grid at init and list the missing ones. |
| B4 | *(Done 2026-10-12: `wide_event_min_stations` (5) exempts an event picked by enough distinct stations from the core rule; measured against the reviewed catalog: rescues 0 rejected; catalog step warns when a core station is retired/unknown.)* **Triage core stations are the four legacy ones.** Events seen mainly by new stations are auto-rejected. | `5-catalog/config.yaml:44`, `lib/triage.py:35-37` | Re-derive after a few weeks of data, or rule on N of all stations. |
| B5 | *(Done 2026-10-12: `lib/geometry.py` derives everything from `NETWORK_CENTER_*`/`NETWORK_HALF_WIDTH_DEG` = 1.0°; association config may not set geometry keys; NLLoc extents derived (94/111 km, grids unchanged); map reads `/api/study_area`; a station outside the box fails validation.)* **Geometry is defined in four places with two values.** Association box ±1.0°, `lib/constants.py` 0.7°, NLLoc grid ±94/±111 km, dashboard map rectangle. A station outside the box breaks Grid2Time and is invisible on the map. | `4-association/config.yaml:21-34`, `lib/constants.py:26-32`, `lib/location/nlloc_config.py:34-38`, `app.js:296-304,375-378` | One source of truth for network extent, derived from `stations.json`. |
| B6 | *(Done 2026-10-12: PhaseNet input split into station groups (`phasenet_station_group_size`, default 6), each padded to the day's common span; picks identical (prob/amp exact, times within 4 ms); peak RSS 25.6 GB -> ~15 GB and flat with station count.)* **Detection memory scales with stations.** All raw files of the day are merged into one stream (~2.5 GB at 24 stations) and PhaseNet reads the whole day as one batch (~15 GB estimated at 24). Processing auto-sets workers to `min(cpu, n_stations)` = 24 here. | `detect.py:161-176`, `phasenet/predict.py:84-85`, `process.py:392-397` | Measure RSS on the next run; feed PhaseNet per station; cap workers by a memory budget. |
| B7 | **Dashboard caches stations forever; 3C channel list is hard-coded; KIDD is the default helicorder; stations keyed by bare code.** | `dashboard/deps.py:62-86`, `waveforms.py:640`, `app.js:1673,2151` | Watch `stations.json` mtime; derive channels per station; key by `net.sta`. |
| B8 | **Map and notifications drop events after 500 rows.** The frontend asks for the first 500 in file order (oldest first). | `app.js:1456,1693`, `routes/catalog.py:90-104` | Sort by time descending, or add a bbox/date endpoint. |
| B9 | **Adding a station is undocumented.** Today it is: edit `stations.json` (7 fields), rebuild grids, decide core stations, restart dashboard, gap-fill, fix three stale docs. | — | A `RUNBOOK.md` procedure, and automate the grid check. |

## C. Maintainability for the next lab member

- **Stages are scripts, not modules.** Five stage scripts re-declare the same `DEFAULTS`, path and logging keys, and the scheduled-window idea under four names; `ingest.py` reimplements `lib/pipeline_stage.resolve_time_window`. Tests import them via `sys.path` hacks; `run_pipeline.py` itself has no tests. Move stage logic into `lib/stages/*.py` with thin CLI wrappers. (`ingest.py:42-59,587-597`, `process.py:47-72`, `detect.py:39-54`, `associate.py:59-95`, `catalog.py:32-41`)
- **Config disagrees with code.** `lib/constants.py` and `dashboard/locators.py` carry their own geometry, DBSCAN eps and velocity values that differ from the YAML the pipeline runs; `dashboard/routes/review.py:211-219` hard-codes the ML constants. The dashboard relocates with different physics than the pipeline. Load `4-association/config.yaml` everywhere. No config schema validation: a typo'd key silently falls back to a default (`lib/config.py:15-51`).
- **Event IDs are positional** (`ep{date}-{index}` from GaMMA's index, which is non-deterministic with `ncpu>1`); 130 lines of `restore_reviewed_events` exist to compensate (`catalog.py:68-70`). Content-based IDs would remove that class of bug.
- **No schema versions** on `catalog.csv`, the SQLite tracker, or `stations.json`.
- **Dashboard blocks its own event loop.** Heavy handlers are `async def` with synchronous ObsPy reads, FFTs and `subprocess.run` (backup up to 120 s, NLLoc 60 s), freezing every reviewer. `/api/errors` re-reads ~15 MB of logs every 10 s per client; `/api/disk` and `/api/stats` walk the RAID trees every 30–60 s. (`routes/admin.py:46-72`, `review.py:180`, `waveforms.py:747`, `system.py:238-284`, `catalog.py:139-172`)
- **`app.js` is 4,089 lines, 123 global functions, five pollers, no tests.** Split into modules (map, catalog, review, waveforms, helicorder) before it grows further.
- **Dashboard logging is unconfigured**: `logs/app.log` is empty while the app runs.
- **CI runs only pytest**; lint is `if: false`; mypy covers `lib/` only; the weekly smoke test cannot pass (needs the phasenet env). CI installs unpinned pip packages unrelated to `environment.yml`.
- **Packaging is nominal.** `pip install .` does not produce a runnable install (stage dirs are not packages; `run_pipeline` not in `py-modules`); `pyproject.toml` dependencies disagree with `environment.yml`; `environment.yml` pins numpy twice. PhaseNet weights have no recorded provenance. The `phasenet` env file is a 165-line export of EOL packages.
- **Docs are stale**: README says 11 stations (14), documents a cron deployment that no longer exists, `MIGRATION.md` describes a layout that differs from the real one. There is no runbook for restart, restore, re-run a day, or add a station.
- **Backups**: four scripts with hard-coded paths and overlapping scope; the only off-RAID copy of the waveforms depends on someone plugging in the USB drive, and nothing records or alerts on when that last happened.

## D. Security (relevant because the dashboard is on the tailnet)

- WebSocket `/ws/status` has no auth; middleware comment claims otherwise (`middleware.py:78-80`, `system.py:121-140`).
- Lockout and rate limit key on the socket peer, which is 127.0.0.1 for every Tailscale user: one person's five bad passwords lock everyone out (`middleware.py:85,175`).
- Body-less POSTs (`/api/pipeline/stop`, `/api/admin/backup`, `/api/admin/validate`) are CSRF-able under basic auth; backup/validate are not local-only (`pipeline.py:366`, `admin.py:67,75`).
- Waveform path params are unvalidated before filename interpolation (`waveforms.py:260-296`).
- The launcher bakes the password in plain text (mode 700 now mitigates).

## E. Keep (done well)

Atomic catalog writes with fsync; `detect.py` leaving failed days unmarked; rebuild snapshot + fail-fast; append-only `reviews.jsonl` design; `restore_reviewed_events` reasoning; thread-safe WAL SQLite tracker; per-host circuit breaker and 429/503 backoff; half-open day semantics in `iter_days`; consistent UTC; `check_critical_imports`; auth design (loopback bypass, constant-time compare, admin local-only, refuses non-loopback bind without auth); watchdog policy is pure and tested; spectrogram cache quota; routers split by domain with a tiny `app.py`; config YAMLs explain why values deviate from the audit; `prune_raw`/`dedup_catalog` default to dry-run.

## F. Proposed order of work

1. **Hardening week (A1–A12, A7 first).** Small, local changes; each one testable; no behaviour change for good days. Do this before any new station.
2. **Station-readiness (B1–B9).** Config validation, grid check, parallel ingest, memory measurement, runbook. Then add one station as a rehearsal.
3. **Structure (C).** Stages into `lib/stages`, one config loader with validation, content-based event IDs with a migration, CI lint back on, docs rewritten around a runbook.
4. **Dashboard (C, D).** Threadpool for heavy handlers, caches with mtime, WS auth, forwarded-for keying, CSRF header, `app.js` split.

Each phase leaves the system runnable; none requires stopping the pipeline for more than a restart.
