# STATUS — elpaso-quake-network engineering work

## RESUME HERE

- **State (2026-10-16):** pipeline LIVE under `seismiclab`, daily run 02:00 UTC. Event ids are
  time-based and the 152 old ones were migrated (log 2026-10-16). PhaseNO second detector and the
  regional-quake triage rule are merged; **PhaseNO is OFF** (`3-detection/phaseno/config.yaml`).
- **Where:** Mac Studio, `/Users/seismiclab/Marc/Elpaso/elpaso-quake-network`, branch `main`.
  Waveforms on `/Volumes/VelascoRAID1` (`output/1-raw`, `output/2-processed` are symlinks);
  see `../docs/MACSTUDIO_INSTALL.md` for the original install.
- **Rules:** small commits with tests; no behaviour change for good days; restart the pipeline
  only right after a day finishes (or while it is "waiting"). Never run `scripts/prune_raw.py`.
  `deploy/app/local.env` holds the dashboard login and SMTP password — never commit or print it.
  Commits carry no Co-Authored-By / Claude-Session lines. `dashboard/static/` is worked on in a
  separate session.
- **Constraint:** Raspberry Shake FDSN rejects concurrent requests — AM ingest stays serial.
  Keep watching `logs/ingest.log` for "too many requests" (revert `station_delay_seconds` to 5).
- **Next check:** after the 2026-10-17 02:00 UTC run, the first on the new id code: new events
  are `ep…T…`, `ingested_days.json` gains the day, no "Near-duplicate" in `logs/catalog.log`.
- **Open items, in order:**
  1. **PhaseNO: enable** (`enabled: true`, no restart) and decide on a backfill (~36 h GPU for
     all days). RUNBOOK "PhaseNO second detector".
  2. Public site `../elpaso-seismic` is not pushed (local data commits + the old-id redirect;
     origin has a redesign PR, no file overlap) → `git pull --rebase`, push. The pipeline's daily
     push fails until then.
  3. ElPaso2021 nodal campaign is a first pass — dedicated association config + re-run of
     2021-06-05..06-16 before anyone reviews those events.
  4. RADA1 site fix (~2 weeks out): then re-run Sep 26–Oct 2 2026.
  5. The 10 new stations (~2 weeks out): `scripts/add_station.py add ...`, then `--gap-fill`
     (without `--archive-only` is safe now; see RUNBOOK "Event ids").
  6. Phase 3: hourly rolling pass (see log 2026-10-12 "latency").
  7. Butterfield M1.4 (2026-06-25): picked by PhaseNO, associated by neither — investigate.

## Phase 1 and 2

Phase 1 (A1–A14, hardening) and Phase 2 (B1–B9, stations) are complete; each item is in the log.

## Log

- 2026-10-03: review written (`docs/ENGINEERING_REVIEW.md`), plan agreed, Phase 1 started.
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
- 2026-10-12: RADA1 check for Marc (`../research/station_checks/RADA1_2026-10-12/`): data loss
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
  one unreviewed event changes (ep20260915T084856, M2.1, 6 stations, 1 core: auto_reject -> ok).
  `check_core_stations` warns at catalog time if a core station is retired or unknown. 304 tests.
- 2026-10-12: **B7 done.** Dashboard: `load_stations` reloads when `stations.json` changes
  (mtime), so adding/retiring a station needs no restart; each station carries
  `channel_codes` derived from `lib.models.channel_layout` and the 3C waveform fan-out
  uses them (HH? for KIDD, Z-only for RS4D/RS1D) instead of a fixed EHZ/EHN/EHE; the
  helicorder defaults to the first active broadband station (KIDD today, not hard-coded);
  map markers are keyed NET.STA. 306 tests; app.js parsed with the system JS engine.
- 2026-10-12: **B8 done.** `/api/catalog` returns newest-first when no sort is given
  (the file is in day order, so a capped page used to return the oldest rows) and accepts
  `per_page=0` for all rows; the map/notification fetch and the helicorder overlay use it
  (helicorder now asks for its one day server-side). 308 tests.
- 2026-10-12: **B9 done — Phase 2 complete.** `RUNBOOK.md` (where things are, daily rhythm,
  add/retire a station, study area, restart, failure table, tests);
  `scripts/add_station.py add|retire` (entry from model template, validation incl. study
  area, one FDSN metadata request, atomic write, NLLoc grid rebuild, next steps printed);
  `run_pipeline.py --gap-fill --archive-only` runs ingest+process only for a new station's
  history so reviewed days are never re-associated (event ids were day-indexed then).
  312 tests.
- 2026-10-12: app restarted (B7/B8 live: `channel_codes`, newest-first catalog, `/api/study_area`,
  `nlloc_missing_grids: []`); `phase2-stations` fast-forwarded into `main`, 312 tests, pushed.
- 2026-10-12: **ElPaso2021 nodal campaign imported** (Marc's request). Originals: SAC, 1000 Hz
  integer counts, 49 Fairfield ZLand 3C nodes (4B.101–150, no 125), 2021-06-05 07:00 → 06-15
  07:00 UTC, on the RAID at `Data/ElPaso/deployments/ElPaso2021_nodal/sac` (481 GB; the earlier
  100 Hz miniSEED derivative is kept beside it). Corner frequency confirmed at ~5 Hz from the
  microseism-band noise ratio against EP.KIDD (`../research/station_checks/nodes_2021_response/`), so the
  StationXML carries a 5 Hz / 0.7-damped geophone response with the list's 1.507e9 counts/(m/s)
  (checked against theory at 0.2–40 Hz). One E-component day (06-06 07:00 → 06-07 07:00) is lost
  on every node: the export duplicated the previous file. Import (6 workers) 19 min → 24 GB of 100 Hz
  int32 day files; NLLoc grids rebuilt for 63 stations; steps 2–5 for 06-05..06-15 took 26 min
  (PhaseNet 18 min for 11 days × 49 stations in groups of 6). Result: 3–4k picks/day, 31 events,
  15 `ok` (>= 5 stations) and 16 auto-rejected (no 2021 core stations exist, so only the wide-event
  rule applies). USGS (incl. TexNet) has nothing in the box for the window; 24 of 31 locate at depth
  0 and 16 are in local daytime — review before trusting; ML values are from the Shake-tuned scale.
  Five samples on 4B.129/134 tripped the 2^23 guard (true spikes; masked). Three evalresp
  "sensitivities differ by more than 5 percent" lines in `process.log` could not be reproduced on
  any trace and every XML's reported vs computed sensitivity agrees — noted, harmless. Dashboard:
  waveform lookup now finds GPZ/GP1/GP2 for an EHZ/EHN/EHE request and the all-station view only
  lists stations active that day. App restarted (dashboard code). 320 tests.
- 2026-10-14: **Dashboard front end.** Auto-rejected events (triage verdict, not yet human-reviewed)
  are hidden on the monitor map by default; an "Auto-rejected" layer toggle shows them as hollow dashed
  rings, and they no longer raise a "New event" toast (catalog unchanged: Status filter still lists
  them). Button cleanup: one `.seg` segmented-control style shared by the date presets, basemap
  picker, map layer toggles, pick mode and undo/redo; map toggles use the `.active` class instead of
  inline styles; Export CSV / QuakeML collapsed into one Export dropdown (same IDs, File menu still
  works); catalog pagination uses `.btn-sm`; review header has coloured Confirm/Reject outlines and
  a × close at the far right. Static files only — no app restart needed (served from disk).
- 2026-10-14: **PhaseNO vs PhaseNet test — keep PhaseNet.** Both pickers through the same GaMMA
  and triage on all 97 reviewed events (±10 min windows) and 6 full days; details and re-run
  steps in `../research/phaseno-eval/FINDINGS.md`. Confirmed events: 41/47 PhaseNO vs 40/47 PhaseNet
  (different misses, union 46/47). Full days: PhaseNO 66 events / 21 in queue vs 6 / 5; its
  extras are mostly Delaware Basin M2.8–3.8 quakes (Whites City/Toyah) that clear the
  wide-event triage rule. PhaseNO recovers more human-added picks (P 41/48 vs 35/48) and
  runs ~7 min/day on the M2 Ultra GPU. Open idea: tag events that follow a USGS/TexNet
  Delaware Basin quake by 15–60 s (12 of 50 rejected events are these).
- 2026-10-15: **Fewer alert emails.** Every day since Oct 7 is "partial" (RADA1), so the per-day
  "ingested with gaps" and the 7-day "remains incomplete" emails fired daily; both are now log
  lines only (`run_pipeline.py`). "No new events in 48 h" is logged, not emailed (quiet days are
  normal; `lib/monitoring.py`). Still emailed: station dark 3 days / back, day skipped, watchdog
  gave up, disk >80 %, M≥4. The dark-station check that failed on the 4B "node" instrument has
  worked since the 2026-10-14 16:39 restart (fix in 932648e). Tests pass; pipeline restarted by
  Marc 2026-10-15 10:21 MDT.
- 2026-10-15: **PhaseNO round 2** (`../research/phaseno-eval/FINDINGS.md` "Round 2", 20 full days, ComCat
  regional check). PhaseNO alone or with a higher threshold is worse (0.6 already loses 3
  confirmed). Best: PhaseNet unchanged + PhaseNO events added only when corroborated by ≥3
  PhaseNet picks + regional-quake rule → 45/47 confirmed in windows, 15/17 on full days,
  ~0.85 queue items/day. Recommendation recorded as open item 1. Local earthquakes still
  untested (3 ComCat events in 150 km; M1.4 Butterfield 2026-06-25 picked well by PhaseNO but
  not associated by either).
- 2026-10-15: account migration notes written (`../docs/ACCOUNT_MIGRATION.md`).
- 2026-10-15: **moved to account `seismiclab`**, workspace `/Users/seismiclab/Marc/Elpaso`; app at
  `~/Applications/El Paso Monitor.app`, pipeline restarted 13:16 MDT, 320 tests pass. Workspace
  reorganised: side analyses under `../research/` (phaseno-eval, events, station_checks,
  snr_analysis, concert_seismology), notes in `../docs/`, one-off logs in `../logs/`,
  `dissertation-figures` renamed `../figures`; review docs here moved to `docs/`.
- 2026-10-16: **regional rule + PhaseNO merged to main** (9 commits `e824bb3`..`5844a94`, ff).
  Regional triage: auto-reject an origin 5–90 s after a ComCat M≥3.0 quake 150–350 km away
  (Marc's thresholds; M≥2.0 would tag 11 instead of 8 of 50 rejected, 0 confirmed either way).
  `--retriage` run on the live catalog: 9 labels changed (5 on reviewed-rejected events, 4
  unreviewed → auto_reject), review_status untouched; backup `catalog.csv.bak-*-premerge`.
  ComCat cache `output/5-catalog/comcat_regional.csv`. Also: catalog `--retriage` now holds the
  catalog lock. PhaseNO: orchestrator selects steps by name; new optional step `phaseno`
  (between associate and catalog; off unless config `enabled: true`; failure = alert, day
  completes) runs `3-detection/phaseno/step.py` → `output/phaseno/{3-picks,4-events,4-additions}`;
  catalog ingests additions as `po…` ids with `detector`, `corroborating_picks`; dashboard tag.
  Merge rule `lib/detector_merge.py` reproduces analyze2.py exactly on the Round 2 data. End-to-end
  in a sandbox on 2025-11-28: PhaseNO 4 min, 11 events, 1 added (18:58:04 UTC, 3 corroborating
  picks, triage ok), existing rows unchanged. Tests: 385 pass. App restarted 09:29 MDT.
- 2026-10-16: **event ids are time-based** (`{ep|po|tm}YYYYMMDDTHHMMSS`, origin of the first
  solution, `lib/event_ids.py`; commits `b40b5dd`..`75d1ca3`, `c4cd3ce`, `6554b01`..`01c4252`).
  On `--rebuild` every re-found event (same prefix, origin ±10 s, one-to-one) keeps its id,
  reviewed or not (`carry_over_ids`); with every day's GaMMA indices reversed the old code left
  60 ids naming a different event (20 reviewed), the new code 0, so re-detecting a new station's
  history is now safe. Which days are ingested is kept in `output/5-catalog/ingested_days.json`
  (ids no longer say: an event seconds before midnight sits in the next day's file).
  **All 152 old positional ids migrated** (Marc: consistency matters) with
  `scripts/migrate_event_ids.py`: catalog/assignments byte-exact apart from ids, 96 `id_migration`
  records appended to reviews.jsonl, old -> new in `output/5-catalog/id_aliases.csv`; templates
  renamed after their event by origin time (8/34 June names had drifted: duplicates -> `<id>_2`).
  Backups `output/{5-catalog,templates}/id-migration-backup-20261016T172022Z/`. Old ids still
  work: dashboard search, `/api/event/<old id>` + "Former ID" row, public site
  `data/aliases.json` (old links checked in headless Chrome). `validate_locator.py` finds the day
  by origin time. Notes, figures (fig09/13/14) and FINDINGS.md updated to new ids; research
  result CSVs keep old ids. Old spectrogram cache entries orphaned (regenerated on demand).
  App restarted 11:21 MDT. 416 tests.
