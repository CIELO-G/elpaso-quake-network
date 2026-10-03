# STATUS — elpaso-quake-network engineering work

## RESUME HERE

- **Where:** Mac Studio, `~/Research/Elpaso/elpaso-quake-network`, branch `phase1-hardening`.
  Pipeline is LIVE (El Paso Monitor.app, continuous mode, runs each day at 06:00 UTC).
  Waveforms on `/Volumes/VelascoRAID1`; see `../MACSTUDIO_INSTALL.md` for the install.
- **What:** Phase 1 of `ENGINEERING_REVIEW.md` (hardening, items A1–A12). Plan agreed 2026-10-03.
- **Rules:** small commits with tests; no behaviour change for good days; pipeline restart
  only once at the end of the phase, right after a day finishes. Never run `scripts/prune_raw.py`.
- **Constraint:** Raspberry Shake FDSN rejects concurrent requests — AM ingest stays serial.
- **Next step:** after 2026-10-04 06:00 UTC, check `logs/pipeline_stdout.log`, `output/pipeline_state.json`, `/api/health`, and that `1-downloads.db` now has a `coverage` column with values for day 2026-10-03. Then Phase 2 (`ENGINEERING_REVIEW.md` section B).

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
- [x] Restart pipeline after a day completes (done 2026-10-03 18:45 UTC). [ ] Verify 2026-10-03 end to end after the 2026-10-04 06:00 UTC run.

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
