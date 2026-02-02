# elpaso-quake-network

Local seismic data processing pipeline for 11 Raspberry Shake / broadband stations in the El Paso area.


## Project Layout

```
elpaso-quake-network/
├── stations.json                 # Shared station list (all pipeline steps)
├── environment.yml               # Conda environment (dependencies)
├── run_pipeline.py               # Pipeline orchestrator (single-run & continuous)
├── lib/                          # Shared library code
│   ├── config.py                 #   YAML/JSON config & station loading
│   ├── logger.py                 #   Rotating-file + console logging
│   └── db.py                     #   SQLite download tracker
│
├── 1-ingestion/
│   ├── ingest.py                 # Fetch waveforms & metadata from FDSNWS
│   └── config.yaml
├── 2-processing/
│   ├── process.py                # Instrument response removal & filtering
│   └── config.yaml
├── 3-detection/
│   ├── detect.py                 # PhaseNet phase picking & amplitudes
│   └── config.yaml
├── 4-association/
│   ├── associate.py              # GaMMA phase association & location
│   └── config.yaml
├── 5-catalog/
│   ├── catalog.py                # Merge daily events into running catalog
│   └── config.yaml
│
├── dashboard/                    # Web dashboard (FastAPI + Leaflet)
│   ├── __main__.py               #   Entry point: python -m dashboard
│   ├── app.py                    #   FastAPI routes & API endpoints
│   └── static/index.html         #   Single-page frontend (inline CSS/JS)
│
├── stations/                     # Reference StationXML files (not used at runtime)
├── output/                       # All pipeline outputs (created at runtime)
│   ├── 1-raw/{year}/{jday}/      #   Raw miniSEED waveforms
│   ├── 1-metadata/               #   Cached StationXML files
│   ├── 1-downloads.db            #   Download tracker
│   ├── 2-processed/{year}/{jday}/ #  Response-removed, filtered miniSEED
│   ├── 3-picks/{year}/{jday}/    #   Daily pick CSVs (all stations)
│   ├── 4-events/{year}/{jday}/   #   Event catalogs & pick assignments
│   ├── 5-catalog/catalog.csv     #   Master event catalog (all days)
│   ├── 5-catalog/assignments.csv #   All pick assignments with global event IDs
│   └── pipeline_status.json      #   Live pipeline status (written by orchestrator)
└── logs/
    └── *.log                     # Per-stage log files
```

All pipeline outputs are centralized under `output/` for easy backup and cleanup. The numbered prefixes show which pipeline step produces each directory.

---

## Setup

```bash
conda env create -f environment.yml
conda activate elpaso-quake
```

Dependencies: `obspy`, `pyyaml`, `pytorch`, `pyproj` (conda-forge), `seisbench`, `xdas`, `GaMMA` (pip — GaMMA installed from [GitHub source](https://github.com/AI4EPS/GaMMA)), `fastapi`, `uvicorn`, `pywebview`.

---

## Stations

Edit `stations.json` at the project root. This file is shared across all pipeline steps. The network includes three station types:

| Type | Model | Channels | Detection mode |
|------|-------|----------|----------------|
| RS3D | 3C geophone | EH? (EHZ/EHN/EHE) | 3-component |
| RS4D | 1C geophone + 3C accelerometer | EHZ + EN? | 3C accelerometer (fallback: EHZ vertical) |
| Broadband | 3C seismometer | HH? (HHZ/HHN/HHE) | 3-component |

RS4D accelerometer channels (EN?) are converted to velocity during processing via instrument response removal, making them usable for phase picking alongside geophone and broadband data.

---

## Step 1: Data Ingestion

Fetches raw waveforms and station metadata from Raspberry Shake FDSNWS (and IRIS for broadband stations). Downloads in 1-hour chunks with retry logic and tracks completed downloads in SQLite to avoid re-fetching.

### Configuration

Edit `1-ingestion/config.yaml`. Key settings:

- **chunk_hours** -- size of each FDSNWS request (default: 1 hour)
- **data_latency_hours** -- how far behind real-time to fetch (default: 6 hours)
- **fetch_window_hours** -- width of the time window per run (default: 24 hours)
- **output_dir** -- base directory for all output (default: `output`)

### Usage

All commands run from the project root.

```bash
# Scheduled run (fetch the most recent window)
python 1-ingestion/ingest.py --config 1-ingestion/config.yaml

# Backfill a specific date range
python 1-ingestion/ingest.py --config 1-ingestion/config.yaml --start 2024-01-01 --end 2024-01-07

# Debug logging
python 1-ingestion/ingest.py --config 1-ingestion/config.yaml --debug
```

### Automating with cron

To run daily at 02:00:

```
0 2 * * * cd /path/to/elpaso-quake-network && python 1-ingestion/ingest.py --config 1-ingestion/config.yaml >> logs/cron.log 2>&1
```

The download tracker (`output/1-downloads.db`) prevents re-fetching already-downloaded chunks, so the script is safe to run repeatedly.

---

## Step 2: Waveform Preprocessing

Removes instrument response, applies detrending, tapering, and bandpass filtering to raw miniSEED files. Output mirrors the raw directory layout under `output/2-processed/`.

### Processing Pipeline

For each raw miniSEED file:
1. Read and merge overlapping traces (from chunked ingestion)
2. Detrend (demean, then linear)
3. Taper (Hann window, 5%)
4. Remove instrument response (output: velocity m/s, pre-filt: 0.5-1.0-45.0-49.0 Hz, water level: 60 dB)
5. Bandpass filter (1-45 Hz, 4-pole Butterworth, zero-phase)
6. Write processed miniSEED

### Configuration

Edit `2-processing/config.yaml`. Key settings:

- **filter_freqmin / filter_freqmax** -- bandpass corner frequencies (default: 1.0-45.0 Hz)
- **pre_filt** -- cosine taper corners for response removal (default: [0.5, 1.0, 45.0, 49.0])
- **response_output** -- output units after response removal (default: VEL)

### Usage

```bash
# Scheduled run (process the most recent 24-hour window)
python 2-processing/process.py --config 2-processing/config.yaml

# Process a specific date range
python 2-processing/process.py --config 2-processing/config.yaml --start 2026-01-29 --end 2026-01-29

# Force reprocessing (overwrite existing output)
python 2-processing/process.py --config 2-processing/config.yaml --start 2026-01-29 --end 2026-01-29 --force
```

Skip logic: if a processed file already exists under `output/2-processed/`, it is skipped. Use `--force` to reprocess.

---

## Step 3: Phase Detection & Picking

Runs PhaseNet (via SeisBench) on processed waveforms to identify P- and S-wave arrivals, and measures peak velocity amplitude at each pick. Outputs one CSV per day containing picks from all stations.

### How It Works

1. Loads PhaseNet model once (uses GPU if available, otherwise CPU)
2. Iterates days (outer) x stations (inner)
3. For each station-day, reads processed miniSEED into an ObsPy Stream
4. Runs `model.classify()` to extract P and S picks above threshold
5. Measures peak absolute velocity in a window around each pick
6. Writes all picks for the day to a single CSV

### Channel Logic

- **RS3D**: uses all 3 geophone channels (EHZ/EHN/EHE)
- **RS4D**: uses 3 accelerometer channels (ENZ/ENN/ENE, already velocity after processing); falls back to EHZ if accelerometer data unavailable
- **Broadband**: uses all 3 seismometer channels (HHZ/HHN/HHE)

If a 3C station only has partial channels available, detection proceeds with a warning.

### Output Format

One CSV per day at `output/3-picks/{year}/{jday}/{year}.{jday}.picks.csv`:

```csv
network,station,location,channel,phase,time,probability,model,amplitude,amplitude_channel
AM,R0F2D,00,EHZ,P,2026-01-29T03:14:22.450000Z,0.8700,PhaseNet:original,3.241000e-06,EHZ
AM,R0F2D,00,EHZ,S,2026-01-29T03:14:24.120000Z,0.7200,PhaseNet:original,8.105000e-06,EHZ
```

Columns:
- **network, station, location, channel** -- trace identification
- **phase** -- P or S
- **time** -- pick time (ISO 8601 UTC)
- **probability** -- PhaseNet confidence (0-1)
- **model** -- model identifier (e.g. `PhaseNet:original`)
- **amplitude** -- peak absolute velocity (m/s) in measurement window
- **amplitude_channel** -- channel the amplitude was measured on

### Configuration

Edit `3-detection/config.yaml`. Key settings:

- **phasenet_model** -- SeisBench pretrained weights (default: `original`)
- **p_threshold / s_threshold** -- pick probability thresholds (default: 0.3)
- **classify_overlap** -- sliding window overlap in samples (default: 1500, i.e. 50% of PhaseNet's 3001-sample window)
- **amp_window_before / amp_window_after** -- amplitude measurement window in seconds (default: 0.5 / 2.0)

### Usage

```bash
# Scheduled run (detect the most recent 24-hour window)
python 3-detection/detect.py --config 3-detection/config.yaml

# Detect a specific date range
python 3-detection/detect.py --config 3-detection/config.yaml --start 2026-01-29 --end 2026-01-29

# Force re-detection (overwrite existing CSVs)
python 3-detection/detect.py --config 3-detection/config.yaml --force

# Debug logging
python 3-detection/detect.py --config 3-detection/config.yaml --start 2026-01-29 --end 2026-01-29 --debug
```

Skip logic: if the daily CSV already exists under `output/3-picks/`, the entire day is skipped. Use `--force` to redetect.

---

## Step 4: Phase Association & Location

Groups individual P/S picks from Step 3 into seismic events and estimates hypocenter locations using GaMMA (Gaussian Mixture Model Association). Uses a stereographic projection centered on the network to convert station coordinates to a local Cartesian frame for the association algorithm.

### How It Works

1. Builds a station DataFrame with projected x/y/z coordinates (once)
2. Iterates days, loading that day's picks CSV from Step 3
3. Filters picks below a minimum probability threshold
4. Runs GaMMA association (BGMM or GMM method) with DBSCAN pre-clustering
5. Inverse-projects event locations back to lat/lon
6. Writes event catalog and pick-assignment CSVs

### Magnitude

Event magnitudes are computed as local magnitude (ML) using the Hutton & Boore (1987) attenuation relation, standard for western US regional networks:

```
ML_station = log10(A_wa) + 1.110 * log10(r/100) + 0.00189 * (r - 100) + 3.0
```

Where `A_wa` is Wood-Anderson equivalent displacement amplitude (mm) and `r` is hypocentral distance (km). Velocity amplitudes from Step 3 are converted to Wood-Anderson displacement using a dominant frequency of 5 Hz and the standard WA static magnification of 2080. The event ML is the mean of per-station ML values.

### Output Format

Two CSVs per day under `output/4-events/{year}/{jday}/`:

**Event catalog** (`{year}.{jday}.events.csv`):

```csv
event_index,time,magnitude,magnitude_type,ml_err,latitude,longitude,depth_km,sigma_time,sigma_amp,num_picks,num_ml_sta
0,2026-01-29T00:09:59.580000,1.23,ML,0.15,31.801234,-106.227456,8.50,0.1234,0.5678,6,3
```

**Pick assignments** (`{year}.{jday}.assignments.csv`):

```csv
event_index,network,station,location,channel,phase,time,probability,amplitude,amplitude_channel
0,AM,RDB14,00,EHZ,P,2026-01-29T00:09:58.580000Z,0.7553,6.013024e-05,EHZ
```

### Configuration

Edit `4-association/config.yaml`. Key settings:

- **center_lat / center_lon** -- network centroid for stereographic projection (default: 31.85, -106.40)
- **method** -- association method: `BGMM` (Bayesian) or `GMM` (default: BGMM)
- **min_picks_per_eq** -- minimum picks to form an event (default: 4)
- **min_pick_probability** -- pick filter threshold (default: 0.3)
- **vel** -- 1D velocity model in km/s (default: P=6.0, S=3.46)
- **dbscan_eps / dbscan_min_samples** -- DBSCAN pre-clustering parameters (default: 10s, 3)
- **max_sigma11 / max_sigma22** -- max residual standard deviations for time and amplitude

### Usage

```bash
# Scheduled run (associate the most recent 24-hour window)
python 4-association/associate.py --config 4-association/config.yaml

# Associate a specific date range
python 4-association/associate.py --config 4-association/config.yaml --start 2026-01-29 --end 2026-01-29

# Force re-association (overwrite existing CSVs)
python 4-association/associate.py --config 4-association/config.yaml --force

# Debug logging
python 4-association/associate.py --config 4-association/config.yaml --start 2026-01-29 --end 2026-01-29 --debug
```

Skip logic: if the daily events CSV already exists under `output/4-events/`, the entire day is skipped. Use `--force` to re-associate.

---

## Step 5: Running Event Catalog

Merges daily event files from Step 4 into a single running catalog with globally unique event IDs. The catalog is updated incrementally — re-running appends new days without duplicating existing ones.

### How It Works

1. Discovers all daily event files under `output/4-events/{year}/{jday}/`
2. Loads the existing catalog (if any) to determine which days are already included
3. For each new day, reads the events and assignments CSVs
4. Assigns each event a global ID in the format `ep{YYYYMMDD}-{NNNN}` (e.g., `ep20260101-0007`)
5. Maps pick assignments from daily `event_index` to the global `event_id`
6. Appends new data to the catalog, sorts chronologically, and writes output

### Event ID Format

Each event gets an ID like `ep20260101-0007`:
- `ep` — network prefix (El Paso)
- `20260101` — calendar date (YYYYMMDD)
- `0007` — zero-padded daily event index from Step 4

### Output Format

**Master catalog** (`output/5-catalog/catalog.csv`):

```csv
event_id,event_index,time,magnitude,magnitude_type,ml_err,latitude,longitude,depth_km,sigma_time,sigma_amp,num_picks,num_ml_sta
ep20260101-0007,7,2026-01-01T10:36:53.122,1.20,ML,0.29,31.674656,-106.088666,0.00,1.1208,0.5109,4,3
```

**Pick assignments** (`output/5-catalog/assignments.csv`):

```csv
event_id,network,station,location,channel,phase,time,probability,amplitude,amplitude_channel
ep20260101-0007,AM,R4B41,0.0,EHZ,P,2026-01-01T10:37:01.980000Z,0.8607,1.151308e-06,EHZ
```

### Configuration

Edit `5-catalog/config.yaml`. Key settings:

- **catalog_file** -- output path for master catalog (default: `5-catalog/catalog.csv`, relative to `output_dir`)
- **assignments_file** -- output path for assignments (default: `5-catalog/assignments.csv`, relative to `output_dir`)

### Usage

```bash
# Build or update the catalog (incremental — only adds new days)
python 5-catalog/catalog.py --config 5-catalog/config.yaml

# Force a full rebuild from scratch
python 5-catalog/catalog.py --config 5-catalog/config.yaml --rebuild

# Debug logging
python 5-catalog/catalog.py --config 5-catalog/config.yaml --debug
```

Incremental logic: days already present in the catalog are skipped. Use `--rebuild` to regenerate from scratch.


---

## Pipeline Orchestrator

`run_pipeline.py` runs all 5 steps as subprocesses and writes real-time status to `output/pipeline_status.json` for the dashboard. Supports two modes:

### Single-run mode

Runs a fixed date range once, then exits.

```bash
# Run all 5 steps for a single day
python run_pipeline.py --start 2026-01-01 --end 2026-01-01

# Run only steps 1-3
python run_pipeline.py --start 2026-01-01 --end 2026-01-01 --start-step 1 --end-step 3

# Force reprocessing
python run_pipeline.py --start 2026-01-01 --end 2026-01-01 --force

# Debug logging in all steps
python run_pipeline.py --start 2026-01-01 --end 2026-01-01 --debug
```

### Continuous mode

Processes one day at a time starting from a given date, catches up to the present (with a 6-hour lag), then sleeps until new data becomes eligible. Runs indefinitely until stopped with Ctrl+C.

```bash
# Start from Nov 1 2025 and run continuously
python run_pipeline.py --continuous --start 2025-11-01

# Auto-resume from where it left off (checks output/4-events/ for the last completed day)
python run_pipeline.py --continuous
```

Continuous mode features:
- **Auto-resume**: if `--start` is omitted, detects the last completed day from `output/4-events/` and resumes from the next day
- **Retry logic**: failed days are retried up to 5 times (with 120s between retries); if all retries fail the pipeline **stops** instead of skipping the day
- **Email alerts**: when the pipeline stops due to persistent failures, an email is sent (see Email Alerts below)
- **Timing tracking**: records per-step and per-day timing for the last 100 days (used by the dashboard for throughput/ETA display)
- **Graceful shutdown**: Ctrl+C stops after the current step finishes and writes a `stopped` status

### Email alerts

When a day fails after all 5 retries, the pipeline shuts down and sends an email notification. Configure via environment variables:

```bash
export ALERT_EMAIL_TO="you@example.com"        # recipient (required)
export ALERT_EMAIL_FROM="sender@gmail.com"      # sender (defaults to ALERT_EMAIL_TO)
export ALERT_SMTP_HOST="smtp.gmail.com"          # SMTP server (default)
export ALERT_SMTP_PORT="587"                     # SMTP port (default)
export ALERT_SMTP_PASSWORD="your-app-password"   # Gmail app password or SMTP password
```

For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833) (not your regular password). If `ALERT_EMAIL_TO` is not set, the alert is silently skipped and the pipeline still stops.

### Status file

The orchestrator writes `output/pipeline_status.json` with the current state of each step (pending, running, completed, failed), timing data, and continuous-mode metadata (current day, days completed, days skipped, next run time). The dashboard reads this file for live updates.

---

## Dashboard

A web dashboard for monitoring the pipeline. Shows a station map, event catalog, processing progress, station activity, disk usage, and error log. Updates live while the pipeline runs.

### Running

```bash
# Open in a native desktop window (requires pywebview)
python -m dashboard

# Open in the default browser instead
python -m dashboard --browser

# Custom port
python -m dashboard --port 9000
```

The dashboard serves at `http://127.0.0.1:8000` by default.

### What it shows

- **Station map** (Leaflet + CartoDB Dark Matter tiles) — station triangles colored by health (blue=OK, amber=intermittent, red=offline), event circles sized by magnitude and colored by recency
- **Pipeline step strip** — live status of each step (pending, running, completed, failed) with animation
- **Progress panel** — days processed vs target, catching-up vs live indicator, throughput (avg time per day, ETA), per-step timing breakdown
- **Statistics** — total events, picks, days processed, station count, magnitude range, latest event
- **Events/day chart** — bar chart of event count over time
- **Station activity heatmap** — picks per station per day, color-coded from blue (low) to green (high)
- **Disk usage** — filesystem usage with per-step size breakdown
- **Error log** — most recent errors and warnings parsed from step log files

### API endpoints

All endpoints return JSON (except the HTML root).

| Endpoint | Interval | Description |
|----------|----------|-------------|
| `GET /` | — | Dashboard HTML |
| `GET /api/status` | 2.5s | Pipeline + step status from `pipeline_status.json` |
| `GET /api/stations` | once | Station list from `stations.json` |
| `GET /api/catalog` | 10s | Event catalog from `catalog.csv` |
| `GET /api/stats` | 10s | Summary statistics (events, picks, days, magnitudes) |
| `GET /api/progress` | 10s | Pipeline progress (days per step, percent, continuous-mode state) |
| `GET /api/throughput` | 10s | Per-day timing averages, per-step averages, ETA |
| `GET /api/errors` | 10s | Errors/warnings parsed from log files |
| `GET /api/disk` | 30s | Filesystem and per-step disk usage |
| `GET /api/station_health` | 30s | Per-station raw data availability (last 3 days) |
| `GET /api/event_rate` | 30s | Events per day for the timeline chart |
| `GET /api/station_picks` | 30s | Picks per station per day for the activity heatmap |
| `GET /api/logs/{step}` | — | Tail of a step's log file (step: ingest, process, detect, associate, catalog) |

### Dependencies

The dashboard uses only FastAPI, uvicorn, and pywebview (all in `environment.yml`). The frontend is a single HTML file with inline CSS and vanilla JS — Leaflet is loaded from CDN. No build step required.

---

## Design Notes

- **Idempotent**: every step uses file-existence checks to skip already-completed work; safe to re-run
- **Error isolation**: one bad station or day never crashes the run; errors are logged and skipped
- **Centralized output**: all runtime data goes under `output/` for easy backup to external storage
- **Built once, reused**: PhaseNet weights (Step 3) and station DataFrame + GaMMA config (Step 4) are built a single time and reused across all days
- **Lazy imports**: heavy dependencies (torch, seisbench, pandas, pyproj, gamma) are imported inside functions so `--help` stays fast without them installed
