# STATUS — elpaso-quake-network engineering work

## RESUME HERE

- **Where:** Mac Studio, `~/Research/Elpaso/elpaso-quake-network`. Phase 1 merged to `main` and pushed 2026-10-12; Phase 2 work on branch `phase2-stations`.
  Pipeline is LIVE (El Paso Monitor.app, continuous mode, runs each day at 02:00 UTC since 2026-10-12).
  Waveforms on `/Volumes/VelascoRAID1`; see `../MACSTUDIO_INSTALL.md` for the install.
- **What:** Phase 2 of `ENGINEERING_REVIEW.md` (station readiness, section B). Phase 1 (A1–A14) done.
- **Rules:** small commits with tests; no behaviour change for good days; pipeline restart
  only once at the end of the phase, right after a day finishes. Never run `scripts/prune_raw.py`.
- **Constraint:** Raspberry Shake FDSN rejects concurrent requests — AM ingest stays serial.
- **Next step:** Phase 2 in this order: B7, B8, B9 (B1–B6 done). Watch `logs/ingest.log` for "too many requests" after the 3 s delay change; revert `station_delay_seconds` to 5 if it appears.

## Phase 1 checklist

- [x] A1 review record: cross-process lock shared by catalog step and dashboard; every dashboard save appended to `reviews.jsonl`; backfill sidecar from current catalog.
- [x] A2 ingest exits non-zero on failure.
- [x] A3 association leaves no outputs and exits non-zero on failure.
- [x] A4 resume from last day with events; gap-fill checks picks/events.
- [x] A5 skipped days persisted across restarts.
- [x] A6 partial-hour chunks recorded with coverage and re-fetched.
- [x] A7 alerts: env in local.env + launcher; per-station coverage check; watchdog-exhausted alert; health shows alert config.
- [x] A8 prune_raw.py refuses to run when 1-raw is not local.
- [x] A9 pipeline stop waits for exit; no double pipeline.
- [x] A10 atomic writes for all stage outputs.
- [x] A11 no zero-fill in raw storage.
- [x] A12 NaN amplitude guard.
- [x] A13 (added 2026-10-12) one corrupt chunk cannot kill an ingest run: Steim2 encode failure falls back to INT32/FLOAT64; any other per-chunk exception is recorded as a failed chunk.
- [x] A14 (added 2026-10-12) corrupt samples (|counts| > 2^23, impossible on a 24-bit ADC) are masked as gaps in `lib/waveform_qc.py` before processing and before PhaseNet; raw storage untouched.
- [x] Restart pipeline after a day completes (done 2026-10-03 18:45 UTC). [x] Verified: 2026-10-03 and 2026-10-04 ran end to end on the new code (55 min each).

## Log

- 2026-10-03: review written (`ENGINEERING_REVIEW.md`), plan agreed, Phase 1 started.
  Baseline commit `9d61e10` on main = exactly what is running; branch `phase1-hardening`.
- 2026-10-03 A1 done. New `lib/catalog_lock.py` (flock on `output/5-catalog/.catalog.lock`) held by
  `5-catalog/catalog.py` for its whole merge and by both dashboard save handlers (which now run in a
  worker thread, 503 after 30 s if the pipeline holds the lock). New `lib/reviews.py`; every dashboard
  save/status change appends to `reviews.jsonl` with a `source` field. Backfilled 96 reviewed events
  (`scripts/backfill_reviews_sidecar.py --apply`; pre-backfill copy in RAID `catalog_snapshots/`).
  Sidecar has 97 events: 96 current + 1 legacy July record for an event no longer reviewed in the
  catalog (not investigated). Tests: 224 pass. NOTE: the running app still has the old code until
  the end-of-phase restart.
- 2026-10-03 A2–A5 done (one commit). Ingest now exits 0 / 2 (partial: some data, some
  failures) / 1 (nothing obtained); associate writes nothing and exits 1 on a GaMMA failure.
  Orchestrator: `DayResult(ok, partial, timing)`; ingest exit 2 lets the day proceed but records it in
  the new persisted `output/pipeline_state.json` (`lib/pipeline_state.py`, also holds `days_skipped`,
  loaded at start). Partial days are re-run with --force each caught-up cycle, dropped with an alert
  after 7 days. Resume point = last day with BOTH 2-processed dir and 4-events file. Gap scan also
  flags days with processed data but no events file. Tests: 237 pass. Behaviour note: a day with a
  dead RS server now retries 5× then is skipped and alerted (before: silently "completed" empty).
- 2026-10-03 A6 done. Tracker gains `coverage` (schema v1, auto-migrated on open; legacy rows =
  complete). Ingest computes coverage per chunk from the StationXML channel set, flags chunks under
  `min_chunk_coverage` (0.98) as short → exit 2 → day partial → re-run. Short and no_data chunks are
  re-requested while younger than `refetch_window_days` (7); `ingest.py --force` (now passed by the
  orchestrator on partial re-runs and gap-fill) ignores the age. Complete chunks are never
  re-downloaded. Migration rehearsed on a copy of the live DB (99,154 success / 22,685 no_data rows,
  no failed rows). Tests: 243 pass.
- 2026-10-03 A7 done. `lib/monitoring.py` now owns all delivery: `send_email`, `send_webhook`,
  `notify` (both channels, prints when none configured), `alerting_status`. Launcher exports every
  `ALERT_*` var from `deploy/app/local.env` (placeholders added there, commented out — **Marc must
  fill in an email or Slack webhook and rebuild the app; until then alerts only print to
  pipeline_stdout.log**). `/api/health` reports `alerting: {email, webhook}`. New daily check after
  each completed day: a station with no raw files for 3 consecutive days → one alert; one more when
  it returns (`dark_stations` persisted in pipeline_state.json). Watchdog alerts once when it stops
  restarting the pipeline. Tests: 248 pass.
- 2026-10-03 A8, A9, A12 done. prune_raw.py exits 2 with REFUSING when output/1-raw is a symlink
  or resolves outside the repo. Dashboard stop keeps the handle + PID file until the process really
  exits (reaper thread, SIGKILL after 90 s), so the app cannot relaunch a second pipeline; the
  pipeline_stdout.log fd is no longer leaked per start. NaN amplitudes are skipped in associate and
  compute_ml_station. Tests: 253 pass.
- 2026-10-03 A10, A11 done. `lib/atomic_io.py` gains `atomic_replace`, `atomic_write_rows`,
  `atomic_write_stream`; raw day files, processed files, picks CSV, events/assignments CSVs are all
  written tmp+fsync+replace. Associate writes assignments BEFORE events (events file = done
  marker). Ingest no longer zero-fills gaps: `merge(method=1)` then `split()`, so a day file holds
  one trace per contiguous segment; downstream merges with interpolate as before. Existing raw days
  (written with zeros) are unchanged. Smoke re-check on 2026-09-27: picks identical (2,822),
  38 processed files, no .tmp leftovers. Tests: 258 pass.
  **All 12 code items done. Remaining: restart the app after a day completes, verify end to end.**
- 2026-10-03 18:45 UTC: Phase 1 code live. 2026-10-02 had completed under the old code (55 min,
  2 new events → 114). Stopped pipeline via API, quit app, rebuilt app (launcher now exports
  ALERT_*), relaunched. Health OK, pipeline waiting for 2026-10-03 at 06:00 UTC Oct 4,
  `alerting: {email: false, webhook: false}` until Marc fills in local.env. Branch
  `phase1-hardening` has 7 commits on top of main; not merged or pushed.
- 2026-10-05 verification. Days 2026-10-03/04 completed end to end; catalog 115. Findings:
  (1) **AM.RADA1 is delivering 7–46% of each hour since 2026-10-03** (raw 33 MB/day → 7 MB/day) —
  a station/uplink problem, flagged by the new coverage check; (2) **AM.R5912 has no raw data on
  any day here** (24 no_data chunks every day) — dark alert fired (to log only, no channel set);
  (3) the partial-day re-run re-processed detect/associate/catalog each cycle although the
  re-fetch brought nothing new (~12 min per partial day per cycle). Fixed: ingest now counts
  `improved` chunks and writes `logs/ingest_summary.json`; `_retry_partial_days` runs ingest alone
  and only re-processes when improved > 0. Also: orchestrator and stages now run with `python -u`
  (the stdout log was block-buffered and hid the last day's lines). Tests: 262. Restarted again.
- 2026-10-12: old raw archive imported from Marc's second drive onto the RAID
  (10,591 files, 169 GB, `rsync --ignore-existing`; log `../raw_import_2026-10-12.log`).
  `1-raw/` is now continuous 2025/250 -> today. RAID copies kept where they were newer
  (EP.KIDD days 115/116/123/124). Processed/events for those days were already present.
- 2026-10-12: **incident, 2026-10-06 never processed.** Raspberry Shake served an hour of
  AM.R0F2D (02-03 UTC) that failed its Steim2 integrity check; re-encoding the merged
  day-file raised `msr_encode_steim2: Unable to represent difference in <= 30 bits`,
  a bare `Exception` from libmseed that escaped the worker thread and aborted ingest
  (exit 1) for all 14 stations on every retry for six days. Fix (A13): `write_day_file`
  falls back to INT32/FLOAT64 when Steim2 cannot encode, and `process_station` now
  catches *any* exception per chunk and records it as `failed`. Tests in
  `tests/test_ingest_write_resilience.py` (268 passing). The affected hour may contain
  a garbage spike from the bad record; detection output for 2026-10-06 02-03 UTC on
  R0F2D should be viewed with that in mind. Day re-run manually
  (`../rerun_2026-10-06.log`); orchestrator picks up the new ingest code on its next
  spawn, no restart needed.
- 2026-10-12: follow-up (A14): the corrupt R0F2D record decodes to three samples near
  5.4e8 counts. Processing and detection both read raw counts, so a bandpass would turn
  each into a ringing wavelet PhaseNet could pick. `lib/waveform_qc.mask_impossible_samples`
  (|x| > 2^23 -> gap, split, then the existing interpolate-merge bridges it) now runs in
  `process.py` and in `detect.py`'s `merge_raw_data`. Verified on the real file: peak
  drops from 5.4e8 to 4.3e4 counts. No effect on clean days (tests). 273 tests pass.
- 2026-10-12: Marc: **AM.R5912 is retired** (last data 2026-02-23); RADA1 stays, he will
  check the hardware. Added `end_date` to the station schema (inclusive last day;
  `lib/models.station_active_on`). Honoured by ingest (skips later windows), the gap
  scan, the dark-station alert and the dashboard (`retired` status, excluded from the
  "N/N up" count). History is kept: old days still re-process with R5912 data.
  `start_date` is now required by validation (first half of B2). 281 tests pass.
- 2026-10-12: **alerts live.** Email via Gmail SMTP to Marc's personal address
  (`ALERT_EMAIL_*` in `deploy/app/local.env`, App Password without spaces — the
  launcher sources the file as shell). Test alert delivered through `notify()`; app
  rebuilt and restarted; `/api/health` shows `alerting.email: true`. Before the
  restart, `pipeline_state.json` was edited: 2026-10-06 moved skipped -> partial
  (it completed manually), AM.R5912 removed from `dark_stations`.
  Dashboard loader now passes `end_date` through (`/api/station_health` -> `retired`).
- 2026-10-12: `gh auth login` done (GarciaMarc41). origin/main had six old-Mac commits from
  2026-10-02 whose content was already inside the baseline snapshot; joined with
  `merge -s ours`, rebased the 13 Phase 1 commits on top, 282 tests, pushed `main`.
  PhaseNO checkpoint stays gitignored (on disk + RAID backup).
- 2026-10-12: **B2 done** on `phase2-stations`. A station entry is now validated when loaded:
  dates, `instrument` (a geophone/seismometer with positive sensitivity) and a derivable
  channel layout, each error naming the station. `build_phasenet_stations` and the
  orchestrator use the shared helpers instead of hard-coded `instrument["geophone"]`
  lookups; output for the 14 real stations is unchanged (test). 286 tests.
- 2026-10-12: **B3 done.** `NLLocLocator` checks P and S grids for every station; a station
  without grids is excluded from relocation and listed in `missing_grids` (shown in
  `/api/health` as `nlloc_missing_grids` after the next app restart). All 14 current
  stations have grids. 290 tests.
- 2026-10-12: **B5 done.** The study area is defined once (`lib/constants.py`, ±1.0°, the
  value that was already in effect everywhere; the 0.7° default was stale). `lib/geometry.py`
  derives the NLLoc extents (94/111 km, identical to the built grids — test checks the
  grid header), `/api/study_area` feeds the map rectangle, `4-association/config.yaml`
  no longer carries geometry and is refused if it does, and a station outside the box is
  rejected at load with its coordinates. GaMMA box verified identical. 297 tests.
- 2026-10-12: **B1 done** (Marc: do 1 and 2, try 3 at 3 s). Chunks are buffered per
  station-day and written once; tracker rows are recorded only after the write (a crash
  in between re-fetches, never a phantom "success"); no sleep after a "no data" reply;
  `station_delay_seconds` 5 -> 3. Requests remain strictly serial. Live check on
  AM.RDB14 2026-10-11 in a scratch dir: 24/24 chunks, output byte-identical to the
  archive, 202 s for the station-day (was ~240), no 429s. Pre-existing quirk kept: the
  sample at exactly 00:00:00 spills into a 1-sample file for the next day. 300 tests.
- 2026-10-12: **latency stage 1** (Marc). `DATA_LATENCY_HOURS` 6 -> 2: the day now runs at
  02:00 UTC (20:00 local) instead of 06:00. Measured server lag: Raspberry Shake ~30 min,
  IRIS real time. Event latency 3-27 h (was 7-31). Stage 2 (hourly rolling pass,
  time-based event IDs, ~1-2 h latency) is planned as Phase 3 after the station work;
  stage 3 (SeedLink streaming) only if minutes are ever needed.
- 2026-10-12: RADA1 check for Marc (`../station_checks/RADA1_2026-10-12/`): data loss
  since ~Sep 20, 50–85 % missing since Oct 3, identical on all 3 channels, arriving
  samples normal; points at Pi/power/SD/uplink, not the sensor. Pre-Phase-1 zero-fill
  hid it and produced ~1,000 false picks/day Sep 26–Oct 2 (days may deserve a re-run
  once the station is fixed).
- 2026-10-12: **B6 done.** Detection writes one merged mseed per group of
  `phasenet_station_group_size` stations (default 6) and lists them all for one PhaseNet
  call; PhaseNet holds one file at a time. Groups are padded to the day's common time span,
  otherwise PhaseNet's 30 s windows cut at different offsets and ~9 % of picks moved.
  Verified on 2026-10-11 (13 stations): single file vs groups of 4 -> 1,939 picks both,
  probabilities and amplitudes exact, pick times within 4 ms (sub-sample). Peak RSS
  (`/usr/bin/time -l`): one file 25.6 GB, groups of 4 12.4 GB, of 8 17.7 GB. At 24 stations
  the single file would have been ~47 GB. Runtime unchanged (42-65 s/day). 302 tests.
- 2026-10-12: **B4 done.** Triage: an event picked by >= `wide_event_min_stations` (5) distinct
  stations is never auto-rejected by the core-station rule, so events carried by the new
  stations reach a reviewer. Against the reviewed catalog (46 confirmed / 50 rejected) the
  override rescues none of the 33 rejected events the core rule catches. On the live catalog
  one unreviewed event changes (ep20260915-0002, M2.1, 6 stations, 1 core: auto_reject -> ok).
  `check_core_stations` warns at catalog time if a core station is retired or unknown. 304 tests.
