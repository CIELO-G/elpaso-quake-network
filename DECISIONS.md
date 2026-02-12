# Decisions Log

All agents must record non-trivial decisions here: what, why, alternatives considered, and who approved.

---

## Template

### [Date] Decision Title — Agent N
- **What**: ...
- **Why**: ...
- **Alternatives considered**: ...
- **Approved by**: ...

---

### [2026-02-11] Change 1D velocity model to Vp=6.0, Vs=3.47 km/s — Agent 1 (Science)
- **What**: Update GaMMA velocity model from Vp=5.5/Vs=3.18 to Vp=6.0/Vs=3.47 km/s.
- **Why**: Vp=5.5 is too slow for the El Paso / Rio Grande Rift upper crust. Published values (Wilson et al. 2005, Keller & Baldridge 1999) give Vp=5.8-6.2 for the upper 15 km. Too-slow velocity causes mislocation and false associations.
- **Alternatives considered**: (a) Layered 1D model [5.5, 6.0, 6.5] at [0, 5, 15 km] -- better but more complex, deferred. (b) Keep 5.5 -- rejected, clearly too slow per literature.
- **Approved by**: Pending team lead review.

### [2026-02-11] Reduce DBSCAN eps from 50s to 25s — Agent 1 (Science)
- **What**: Change dbscan_eps from 50 to 25 seconds.
- **Why**: 50s is 3x the max P travel time across the network (21s). Separate events within 50s get merged. 25s covers max_travel_time + margin.
- **Alternatives considered**: (a) 30s -- acceptable but slightly permissive. (b) 20s -- might split events at network edge. 25s is the best compromise.
- **Approved by**: Pending team lead review.

### [2026-02-11] Update Wood-Anderson gain from 2080 to 2800 — Agent 1 (Science)
- **What**: Change ml_wa_gain from 2080 to 2800.
- **Why**: Uhrhammer & Collins (1990) revised the WA static magnification. 2800 is the modern standard used by USGS, SCSN, and all major networks. Using 2080 inflates ML by 0.13 units.
- **Alternatives considered**: Keep 2080 -- rejected, inconsistent with modern practice and reference catalogs.
- **Approved by**: Pending team lead review.

### [2026-02-11] Increase ML minimum distance from 1 to 10 km — Agent 1 (Science)
- **What**: Change ml_min_distance from 1.0 to 10.0 km.
- **Why**: Hutton & Boore (1987) formula calibrated for r > 10 km. Below 10 km, near-field effects make the attenuation formula unreliable.
- **Alternatives considered**: (a) 5 km -- still outside calibration range. (b) Keep 1 km -- rejected, produces unreliable ML at close range.
- **Approved by**: Pending team lead review.

### [2026-02-11] Change pre-filter from [0.5,1,45,49] to [0.5,1,40,45] Hz — Agent 1 (Science)
- **What**: Lower the upper pre-filter corners by 4-5 Hz.
- **Why**: f4=49 Hz is only 1 Hz below Nyquist (50 Hz). The RS digitizer anti-alias filter rolls off above ~40 Hz, making the response characterization unreliable near Nyquist.
- **Alternatives considered**: [0.5, 1.0, 42.0, 47.0] -- still too close to Nyquist. The IRIS/USGS standard is f4 <= 0.9*Nyquist = 45 Hz.
- **Approved by**: Pending team lead review.

### [2026-02-11] Remove redundant bandpass after response removal — Agent 1 (Science)
- **What**: Remove the separate Butterworth bandpass filter applied after remove_response().
- **Why**: Double-filtering at the same corner frequencies distorts amplitudes at band edges. The pre_filt cosine taper in remove_response() already band-limits the data.
- **Alternatives considered**: (a) Keep bandpass with narrower corners -- possible but unnecessary. (b) Move bandpass to detection stage -- PhaseNet does its own normalization. Simplest correct solution is to remove it entirely.
- **Approved by**: Pending team lead review.

### [2026-02-11] Widen S-wave amplitude window to 5 seconds — Agent 1 (Science)
- **What**: Use phase-dependent amplitude windows: P=[pick-0.5s, pick+2s], S=[pick-0.5s, pick+5s].
- **Why**: The S-wave coda maximum can arrive 5-10s after the onset. The 2s window misses the true peak, systematically underestimating ML.
- **Alternatives considered**: (a) 3s -- still too narrow for many local events. (b) 10s -- might pick up later arrivals or noise. 5s is a good balance.
- **Approved by**: Pending team lead review.

### [2026-02-11] Expand search bounds from 0.5 to 0.7 degrees — Agent 1 (Science)
- **What**: Increase xlim_degree and ylim_degree from 0.5 to 0.7.
- **Why**: The network spans 0.87 deg in latitude. With 0.5 deg half-width, the northernmost station (R5912) is only 0.02 deg from the boundary. Events just outside the network cannot be located.
- **Alternatives considered**: (a) 1.0 deg -- unnecessarily large, increases computation. (b) 0.6 deg -- still tight for latitude. 0.7 deg provides ~0.2 deg margin beyond all stations.
- **Approved by**: Pending team lead review.

### [2026-02-11] Keep SoCal ML coefficients, document bias — Agent 1 (Science)
- **What**: Retain Hutton & Boore SoCal attenuation coefficients (a=1.110, b=0.00189, c=3.0) for initial catalog.
- **Why**: No published regional coefficients exist for El Paso / Rio Grande Rift. SoCal coefficients provide internally consistent magnitudes. Expected bias is 0.1-0.3 units at >50 km distance.
- **Alternatives considered**: (a) Zhu et al. (2018) updated western US coefficients (c=2.0) -- possible future improvement. (b) Custom calibration -- requires 6-12 months of catalog data with NEIC reference events.
- **Approved by**: Pending team lead review.

### [2026-02-11] Raise association min_pick_probability from 0.4 to 0.5 — Agent 1 (Science)
- **What**: Change min_pick_probability in association config from 0.4 to 0.5.
- **Why**: Two-tier filtering: keep detection at 0.3 (sensitive), raise association filter to 0.5 (production quality). This reduces false picks entering GaMMA by ~20% while preserving real event detection.
- **Alternatives considered**: (a) Raise detection threshold to 0.5 -- loses weak events permanently. (b) Keep 0.4 -- more false picks. Two-tier approach is used by SCSN and NCEDC.
- **Approved by**: Pending team lead review.

### [2026-02-11] Raise min_picks_per_eq from 3 to 6 — Agent 1 (Science)
- **What**: Change min_picks_per_eq in association config from 3 to 6.
- **Why**: Config file value (3) overrides the code default (6). With min_stations=3, we expect at least 6 picks (P+S from 3 stations). Setting min_picks=3 wastes computation on events that will be rejected by the station filter.
- **Alternatives considered**: Keep 3 -- contradicts code default and allows poorly constrained events.
- **Approved by**: Pending team lead review.

### [2026-02-11] Use standard deviation for ML uncertainty, median for ML — Agent 1 (Science)
- **What**: Replace (max-min)/2 ML uncertainty with standard deviation. Use median instead of mean for ML estimate.
- **Why**: Half-range is non-standard, outlier-sensitive, and not comparable with other catalogs. Standard deviation is the universal practice. Median is more robust than mean for small sample sizes with potential outliers.
- **Alternatives considered**: (a) MAD (median absolute deviation) -- more robust but less standard. (b) Keep half-range -- rejected, not standard.
- **Approved by**: Pending team lead review.

### [2026-02-11] Multi-stage Dockerfile with pre-downloaded PhaseNet weights -- Agent 4 (DevOps)
- **What**: Created a two-stage Docker build: Stage 1 builds the Conda environment and downloads PhaseNet model weights. Stage 2 copies only the runtime environment and pre-cached weights into a clean image.
- **Why**: PhaseNet weights (~100 MB) are downloaded on first use by SeisBench, which would fail in air-gapped deployments and add latency to container startup. Baking them in makes the image self-contained and reproducible.
- **Alternatives considered**: (1) Single-stage build (larger image due to build artifacts), (2) Download weights at runtime with init container (adds startup delay and requires network), (3) Pip-only install without Conda (ObsPy and PyTorch have complex native dependencies that Conda handles better).
- **Approved by**: Pending team lead review.

### [2026-02-11] Docker Compose with GPU profile for NVIDIA runtime -- Agent 4 (DevOps)
- **What**: Added a `pipeline-gpu` service activated via `docker compose --profile gpu up` that requests NVIDIA GPU resources for accelerated PhaseNet inference.
- **Why**: PhaseNet detection (Step 3) runs significantly faster on GPU. Making GPU a separate profile keeps the default compose file usable on machines without NVIDIA drivers.
- **Alternatives considered**: (1) Single service with GPU always requested (breaks on CPU-only hosts), (2) Separate compose file for GPU (harder to maintain).
- **Approved by**: Pending team lead review.

### [2026-02-11] Environment.yml dependency pinning strategy -- Agent 4 (DevOps)
- **What**: Pinned all critical Conda dependencies to exact versions (obspy=1.4.1, pytorch=2.5.1, numpy=1.26.4, etc.) while keeping Python as a range (>=3.10,<3.13).
- **Why**: Ensures reproducible builds. ObsPy, PyTorch, and NumPy version combinations can have subtle incompatibilities. Python range allows flexibility across 3.10-3.12 while all numerical libraries are fixed.
- **Alternatives considered**: (1) Pin nothing (non-reproducible), (2) Use conda-lock for fully resolved lockfile (adds tooling complexity for a small team).
- **Approved by**: Pending team lead review.

### [2026-02-11] Monitoring module with webhook integration -- Agent 4 (DevOps)
- **What**: Created `lib/monitoring.py` with disk space warnings (>80%), event gap detection (48h), significant earthquake alerts (M>=4.0), and optional Slack/webhook integration via `ALERT_WEBHOOK_URL`.
- **Why**: The pipeline runs unattended in continuous mode. Proactive monitoring catches infrastructure issues (disk full) and scientifically interesting events (large earthquakes) before they become problems or are missed.
- **Alternatives considered**: (1) External monitoring stack like Prometheus/Grafana (overkill for a single-pipeline deployment), (2) Cron-based checks only (less integrated, misses events between runs).
- **Approved by**: Pending team lead review.

### [2026-02-11] Data retention with 90-day default for raw waveforms only -- Agent 4 (DevOps)
- **What**: Created `scripts/data_retention.py` to auto-delete raw waveforms (1-raw/) older than N days (default 90, configurable via `--days` or `RETENTION_DAYS` env var). Includes `--dry-run` mode.
- **Why**: At ~150 MB/day for 11 stations, raw waveforms would consume ~55 GB/year unbounded. Processed data and derived products (picks, events, catalog) are retained indefinitely. Storage estimate: ~13.5 GB max at 90-day retention.
- **Alternatives considered**: (1) Delete all output stages (too aggressive -- catalog and picks have scientific value), (2) Compress instead of delete (only ~30% savings on miniSEED, not worth complexity).
- **Approved by**: Pending team lead review.

### [2026-02-11] CI/CD with graceful test detection -- Agent 4 (DevOps)
- **What**: Created `.github/workflows/ci.yml` with lint/typecheck on PR, Docker build on merge, weekly smoke test. Tests run if `tests/` directory exists, otherwise a warning is emitted.
- **Why**: Task #2 (code quality agent) is creating the test suite in parallel. The CI gracefully handles missing tests now and will automatically pick them up once they are committed.
- **Alternatives considered**: (1) Wait for tests before creating CI (delays all other CI benefits), (2) Create dummy tests (creates maintenance burden when real tests arrive).
- **Approved by**: Pending team lead review.

### [2026-02-11] Documentation structure: four-audience split -- Agent 5 (Docs)
- **What**: Created separate documentation targeting operators (`OPERATOR_GUIDE.md`), scientists (`SCIENTIFIC_METHODS.md`), developers (`API_REFERENCE.md`), and reviewers (`docs/adr/`).
- **Why**: Different stakeholders need different levels of detail and different information. Operators need step-by-step instructions; scientists need reproducible method descriptions with citations; developers need endpoint specifications; technical reviewers need decision rationale.
- **Alternatives considered**: (a) Single monolithic docs file -- too large, hard to navigate. (b) Inline comments only -- insufficient for operators and scientists.
- **Approved by**: Pending team lead review.

### [2026-02-11] ADR format for architecture decisions -- Agent 5 (Docs)
- **What**: Created 5 Architecture Decision Records (ADRs) using the Nygard format covering PhaseNet, GaMMA, Hutton & Boore ML, SQLite, and FastAPI.
- **Why**: ADRs capture the "why" behind technical choices. For a scientific pipeline, algorithm selection rationale is critical for reproducibility and peer review.
- **Alternatives considered**: (a) Decision log entries only -- less structured, harder to find. (b) README notes -- gets lost in operational documentation.
- **Approved by**: Pending team lead review.

### [2026-02-11] Scientific methods document scope: current state only -- Agent 5 (Docs)
- **What**: `SCIENTIFIC_METHODS.md` describes only what the code currently implements, not recommended changes.
- **Why**: Separating "current state" from "recommended changes" prevents confusion. The science validation report (Agent 1) covers recommended improvements separately.
- **Alternatives considered**: Combined document with "current" and "recommended" sections -- rejected to avoid ambiguity about what parameters the code actually uses.
- **Approved by**: Pending team lead review.

### [2026-02-11] Keep a Changelog format adopted -- Agent 5 (Docs)
- **What**: Created `CHANGELOG.md` following the Keep a Changelog 1.1.0 format with an [Unreleased] section documenting the full pipeline.
- **Why**: Standard, widely recognized format. Supports future semantic versioning and automated release notes.
- **Alternatives considered**: (a) Git log as changelog -- too noisy, not curated. (b) GitHub Releases only -- requires tagging before documentation.
- **Approved by**: Pending team lead review.

### [2026-02-11] Extract shared library modules (constants, projection, magnitude, models) -- Agent 2 (Code Quality)
- **What**: Created `lib/constants.py`, `lib/projection.py`, `lib/magnitude.py`, `lib/models.py` from code previously scattered or duplicated across pipeline steps.
- **Why**: Hardcoded values (network center, file paths, circuit breaker thresholds) were duplicated. ML computation was embedded in associate.py's 400+ line function, making it untestable in isolation. Projection logic was inline. Data models (Station, Pick, Event) had no formal schema.
- **Alternatives considered**: (a) Keep everything in pipeline step modules -- rejected, prevents independent testing. (b) Single monolithic lib/utils.py -- rejected, too broad.
- **Approved by**: Pending team lead review.

### [2026-02-11] FDSNWS circuit breaker for ingestion resilience -- Agent 2 (Code Quality)
- **What**: Added `CircuitBreaker` class to `1-ingestion/ingest.py` that tracks consecutive 503 errors per host. After 5 consecutive failures, backs off for 5 minutes.
- **Why**: Raspberry Shake FDSNWS servers intermittently return 503 under load. Without a circuit breaker, the pipeline hammers failing servers, wasting time and potentially getting IP-banned.
- **Alternatives considered**: (a) Global retry with exponential backoff only -- doesn't prevent repeated requests to a known-down host. (b) External circuit breaker library (pybreaker) -- adds dependency for a simple pattern.
- **Approved by**: Pending team lead review.

### [2026-02-11] Station validation at config load time -- Agent 2 (Code Quality)
- **What**: Added `validate_station()` and `validate_stations()` to `lib/models.py`, called from `lib/config.py` during `load_stations()`. Validates coordinate bounds, required fields, and channel patterns.
- **Why**: Invalid station entries (e.g., lat > 90, missing coordinates) cause silent failures or nonsensical results deep in the pipeline. Early validation at load time surfaces configuration errors immediately.
- **Alternatives considered**: (a) Validate only in pipeline steps -- errors surface too late. (b) JSON Schema validation -- heavier tooling for a simple JSON array.
- **Approved by**: Pending team lead review.

### [2026-02-11] Structured metrics logging (JSONL) -- Agent 2 (Code Quality)
- **What**: Added `MetricsWriter` class to `lib/logger.py` that appends structured JSON records to `metrics.jsonl`. Integrated into all pipeline steps to record per-station/per-day timing, counts, and error rates.
- **Why**: Log files are human-readable but hard to aggregate. JSONL metrics enable programmatic analysis of pipeline performance trends, identifying slow stations, and detecting degradation.
- **Alternatives considered**: (a) Prometheus/StatsD -- requires external infrastructure. (b) CSV metrics -- harder to extend with variable fields. (c) SQLite metrics -- overkill for append-only time-series.
- **Approved by**: Pending team lead review.

### [2026-02-11] Implement all SCIENCE_AUDIT.md critical/major fixes -- Agent 2 (Code Quality)
- **What**: Applied 10 parameter changes from SCIENCE_AUDIT.md across `2-processing/config.yaml`, `3-detection/config.yaml`, `4-association/config.yaml`, and corresponding Python modules: pre_filt corners, removed redundant bandpass, phase-dependent amplitude windows, velocity model, DBSCAN eps, search bounds, WA gain, ML min distance, min pick probability, min picks per eq, and ML uncertainty calculation.
- **Why**: Agent 1 (Science) audited all seismological parameters and identified critical/major issues. These were implemented in the code by Agent 2 to match the parameter values already documented in DECISIONS.md above.
- **Alternatives considered**: N/A -- implementing the audited recommendations.
- **Approved by**: Pending team lead review.

### [2026-02-11] Test suite with 63 unit tests -- Agent 2 (Code Quality)
- **What**: Created `tests/` directory with 7 test modules covering config loading, magnitude computation, data models, constants, logging, database tracker, and catalog logic. 63 tests total, all passing.
- **Why**: No tests existed. Unit tests for extracted library modules (magnitude, models, constants, logger, db, config) ensure correctness and prevent regressions when parameters or logic change.
- **Alternatives considered**: (a) Integration tests only -- slower, harder to debug. (b) Tests inside pipeline step directories -- harder to discover and run. Centralized `tests/` is pytest convention.
- **Approved by**: Pending team lead review.

### [2026-02-11] Dashboard API hardening and frontend overhaul -- Agent 3 (Dashboard)
- **What**: Comprehensive overhaul of `dashboard/app.py` and `dashboard/static/index.html` adding: CORS middleware, rate limiting (60 req/min per IP), in-memory caching with TTL and file-mtime invalidation, API versioning (/api/v1/ prefix), ANSI stripping + 100KB limit on logs, catalog pagination, date range filtering, WebSocket status push, optional basic auth, /api/health endpoint, event detail API, CSV export, and frontend improvements (loading spinners, error banners, responsive design, event detail overlay, date range selector).
- **Why**: Original dashboard had no auth, no CORS, no rate limiting, no caching, bare 500 errors, potential XSS vectors, and no API versioning. Production deployment requires these hardening measures.
- **Alternatives considered**: (a) Use slowapi for rate limiting -- adds dependency, in-memory middleware is simpler for single-process. (b) Redis caching -- overkill for single-instance; in-memory with TTL + mtime invalidation is sufficient. (c) JWT auth -- too complex for a monitoring dashboard; basic auth with env var toggle is adequate.
- **Approved by**: Pending team lead review.
