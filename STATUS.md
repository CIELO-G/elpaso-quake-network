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

- [ ] A1 review record: cross-process lock shared by catalog step and dashboard; every dashboard save appended to `reviews.jsonl`; backfill sidecar from current catalog.
- [ ] A2 ingest exits non-zero on failure.
- [ ] A3 association leaves no outputs and exits non-zero on failure.
- [ ] A4 resume from last day with events; gap-fill checks picks/events.
- [ ] A5 skipped days persisted across restarts.
- [ ] A6 partial-hour chunks recorded with coverage and re-fetched.
- [ ] A7 alerts: env in local.env + launcher; per-station coverage check; watchdog-exhausted alert; health shows alert config.
- [ ] A8 prune_raw.py refuses to run when 1-raw is not local.
- [ ] A9 pipeline stop waits for exit; no double pipeline.
- [ ] A10 atomic writes for all stage outputs.
- [ ] A11 no zero-fill in raw storage.
- [ ] A12 NaN amplitude guard.
- [ ] Restart pipeline after a day completes; verify next day end to end.

## Log

- 2026-10-03: review written (`ENGINEERING_REVIEW.md`), plan agreed, Phase 1 started.
