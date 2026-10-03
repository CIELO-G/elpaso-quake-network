# STATUS — elpaso-quake-network engineering work

## RESUME HERE

- **Where:** Mac Studio, `~/Research/Elpaso/elpaso-quake-network`, branch `phase1-hardening`.
  Pipeline is LIVE (El Paso Monitor.app, continuous mode, runs each day at 06:00 UTC).
  Waveforms on `/Volumes/VelascoRAID1`; see `../MACSTUDIO_INSTALL.md` for the install.
- **What:** Phase 1 of `ENGINEERING_REVIEW.md` (hardening, items A1–A12). Plan agreed 2026-10-03.
- **Rules:** small commits with tests; no behaviour change for good days; pipeline restart
  only once at the end of the phase, right after a day finishes. Never run `scripts/prune_raw.py`.
- **Constraint:** Raspberry Shake FDSN rejects concurrent requests — AM ingest stays serial.
- **Next step:** see the checklist below; first unchecked item.

## Phase 1 checklist

- [x] A1 review record: cross-process lock shared by catalog step and dashboard; every dashboard save appended to `reviews.jsonl`; backfill sidecar from current catalog.
- [x] A2 ingest exits non-zero on failure.
- [x] A3 association leaves no outputs and exits non-zero on failure.
- [x] A4 resume from last day with events; gap-fill checks picks/events.
- [x] A5 skipped days persisted across restarts.
- [x] A6 partial-hour chunks recorded with coverage and re-fetched.
- [ ] A7 alerts: env in local.env + launcher; per-station coverage check; watchdog-exhausted alert; health shows alert config.
- [ ] A8 prune_raw.py refuses to run when 1-raw is not local.
- [ ] A9 pipeline stop waits for exit; no double pipeline.
- [ ] A10 atomic writes for all stage outputs.
- [ ] A11 no zero-fill in raw storage.
- [ ] A12 NaN amplitude guard.
- [ ] Restart pipeline after a day completes; verify next day end to end.

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
  no failed rows). Tests: 252 pass.
