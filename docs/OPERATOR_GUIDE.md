# Operator Guide

This guide covers installation, configuration, daily operation, monitoring, and troubleshooting for the El Paso seismic processing pipeline.

---

## 1. Prerequisites

### Hardware

- **CPU**: Any modern x86-64 processor (4+ cores recommended for parallel ingestion)
- **RAM**: 8 GB minimum; 16 GB recommended when running PhaseNet detection
- **GPU** (optional): NVIDIA GPU with CUDA support accelerates PhaseNet inference. CPU-only operation is supported but slower.
- **Disk**: At least 50 GB free for a year of continuous data from 11 stations. Raw miniSEED files are the largest consumer (~2-4 MB per station per day).

### Software

- **OS**: Linux or macOS (tested on macOS Darwin 25.x and Ubuntu 22.04)
- **Conda**: Miniconda or Anaconda (Miniforge also works)
- **Git**: For cloning the repository and installing GaMMA from GitHub

### GPU Setup (Optional)

If using an NVIDIA GPU for PhaseNet:

1. Install NVIDIA drivers for your GPU
2. Verify with `nvidia-smi`
3. The `pytorch` package in `environment.yml` will pull CUDA-enabled builds from conda-forge. PhaseNet automatically detects and uses the GPU if available.

---

## 2. Installation

```bash
# Clone the repository
git clone <repository-url> elpaso-quake-network
cd elpaso-quake-network

# Create the Conda environment
conda env create -f environment.yml

# Activate the environment
conda activate elpaso-quake
```

### Verify Installation

```bash
# Check Python and key packages
python -c "import obspy; print('ObsPy', obspy.__version__)"
python -c "import seisbench; print('SeisBench', seisbench.__version__)"
python -c "import gamma; print('GaMMA imported OK')"
python -c "import torch; print('PyTorch', torch.__version__, '| CUDA:', torch.cuda.is_available())"
python -c "import fastapi; print('FastAPI', fastapi.__version__)"
```

---

## 3. Configuration

### 3.1 Station List (`stations.json`)

The station list at the project root defines all stations in the network. Each entry requires:

| Field | Type | Description |
|-------|------|-------------|
| `network` | string | FDSN network code (e.g., `"AM"`) |
| `station` | string | Station code (e.g., `"R0F2D"`) |
| `location` | string | Location code (e.g., `"00"`, or `""`) |
| `channels` | string | Channel pattern (e.g., `"EH?"`, `"E??"`, `"HH?"`) |
| `latitude` | float | Station latitude (decimal degrees) |
| `longitude` | float | Station longitude (decimal degrees) |
| `elevation_m` | float | Station elevation (meters above sea level) |
| `sample_rate_hz` | float | Sample rate in Hz |
| `model` | string | Instrument model (`"RS3D"`, `"RS4D"`, `"broadband"`) |

Optional fields: `fdsnws_url` (overrides default FDSNWS endpoint for a station), `start_date`, `instrument` (metadata block).

The current network has 11 stations: 8 RS3D, 2 RS4D, and 1 broadband (EP.KIDD from IRIS).

### 3.2 YAML Configuration Files

Each pipeline stage has its own `config.yaml`:

| File | Stage | Key Parameters |
|------|-------|----------------|
| `1-ingestion/config.yaml` | Ingestion | `fdsnws_url`, `chunk_hours`, `data_latency_hours`, `fetch_window_hours`, `max_workers`, `max_retries` |
| `2-processing/config.yaml` | Processing | `filter_freqmin`, `filter_freqmax`, `pre_filt`, `response_output`, `water_level` |
| `3-detection/config.yaml` | Detection | `phasenet_model`, `p_threshold`, `s_threshold`, `classify_overlap`, `classify_batch_size` |
| `4-association/config.yaml` | Association | `center_lat`, `center_lon`, `method`, `vel`, `min_picks_per_eq`, `min_stations_per_eq`, `ml_*` |
| `5-catalog/config.yaml` | Catalog | `catalog_file`, `assignments_file` |

All config files share common fields: `stations_file`, `input_dir`, `output_dir`, `log_dir`, `log_max_bytes`, `log_backup_count`.

**Changing the output directory**: Update `output_dir` (and `input_dir` where applicable) in every config file. By default everything goes to `output/`.

### 3.3 Environment Variables (Email Alerts)

For pipeline failure notifications, set these before running:

```bash
export ALERT_EMAIL_TO="operator@example.com"      # Required for alerts
export ALERT_EMAIL_FROM="sender@gmail.com"         # Defaults to ALERT_EMAIL_TO
export ALERT_SMTP_HOST="smtp.gmail.com"            # Default
export ALERT_SMTP_PORT="465"                       # Default (SMTP_SSL)
export ALERT_SMTP_PASSWORD="your-app-password"     # Gmail app password
```

For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833). If `ALERT_EMAIL_TO` is not set, email alerts are silently skipped.

---

## 4. Running the Pipeline

### 4.1 Single Run (One Date Range)

```bash
# Process a single day
python run_pipeline.py --start 2026-01-15 --end 2026-01-15

# Process a date range
python run_pipeline.py --start 2026-01-01 --end 2026-01-31

# Run only specific steps (e.g., steps 1-3)
python run_pipeline.py --start 2026-01-15 --end 2026-01-15 --start-step 1 --end-step 3

# Force reprocessing of existing outputs
python run_pipeline.py --start 2026-01-15 --end 2026-01-15 --force

# Debug logging in all steps
python run_pipeline.py --start 2026-01-15 --end 2026-01-15 --debug
```

**Note**: When `--start` and `--end` are the same date, the orchestrator automatically adjusts the end to the next day (inclusive day processing).

### 4.2 Continuous Mode

Processes days sequentially from a start date, catches up to the present (minus a 6-hour data latency), then sleeps until new data is available. Runs indefinitely.

```bash
# Start from a specific date
python run_pipeline.py --continuous --start 2025-11-01

# Auto-resume from where processing last stopped
python run_pipeline.py --continuous
```

Auto-resume checks `output/2-processed/` for the last completed day and starts from the next one.

**Stopping**: Press `Ctrl+C`. The pipeline finishes the current step, writes a `stopped` status, and exits cleanly.

**Failure behavior**: If a day fails, it is retried up to 5 times with 120-second waits between retries. If all retries fail, the pipeline **stops** (does not skip the day) and sends an email alert.

### 4.3 Running Individual Stages

Each stage can be run independently:

```bash
# Stage 1: Ingestion
python 1-ingestion/ingest.py --config 1-ingestion/config.yaml --start 2026-01-15 --end 2026-01-16

# Stage 2: Processing
python 2-processing/process.py --config 2-processing/config.yaml --start 2026-01-15 --end 2026-01-15

# Stage 3: Detection
python 3-detection/detect.py --config 3-detection/config.yaml --start 2026-01-15 --end 2026-01-15

# Stage 4: Association
python 4-association/associate.py --config 4-association/config.yaml --start 2026-01-15 --end 2026-01-15

# Stage 5: Catalog
python 5-catalog/catalog.py --config 5-catalog/config.yaml
```

Stages 2-4 support `--force` to reprocess existing outputs. Stage 5 supports `--rebuild` for a full catalog regeneration. All stages support `--debug`.

Without `--start`/`--end`, stages 1-4 default to the most recent 24-hour window (offset by a 6-hour data latency).

### 4.4 Scheduling with cron

For unattended daily runs without continuous mode:

```cron
# Run the full pipeline daily at 02:00 UTC
0 2 * * * cd /path/to/elpaso-quake-network && /path/to/conda/envs/elpaso-quake/bin/python run_pipeline.py >> logs/cron.log 2>&1
```

The idempotent skip logic in each stage prevents duplicate processing.

---

## 5. Monitoring

### 5.1 Dashboard

```bash
# Desktop window (requires pywebview)
python -m dashboard

# Browser mode
python -m dashboard --browser

# Custom port
python -m dashboard --port 9000
```

The dashboard (default: `http://127.0.0.1:8000`) shows:
- **Pipeline status**: Running/completed/failed badge with step progress
- **Station map**: Stations colored by health (data presence in last 3 days)
- **Event catalog**: Events plotted on the map with magnitude-scaled markers
- **Progress bar**: Days processed vs. target, with ETA in continuous mode
- **Statistics**: Event count, pick count, magnitude range, station count
- **Events/day chart**: Timeline of daily event counts
- **Station activity heatmap**: Picks per station per day
- **Data completeness**: Raw file counts per station per day
- **Pick quality**: Histogram of pick probabilities
- **Disk usage**: Filesystem usage with per-stage breakdown
- **Error log**: Recent ERROR/CRITICAL/WARNING entries from log files

The dashboard polls the pipeline status file (`output/pipeline_status.json`) and data directories. It works whether or not the pipeline is actively running.

### 5.2 Log Files

All logs are written to the `logs/` directory with rotating file handlers (10 MB per file, 5 backups):

| File | Stage |
|------|-------|
| `logs/ingest.log` | Data ingestion |
| `logs/process.log` | Waveform preprocessing |
| `logs/detect.log` | Phase detection |
| `logs/associate.log` | Event association |
| `logs/catalog.log` | Catalog generation |

Log format: `YYYY-MM-DD HH:MM:SS LEVEL    message`

```bash
# View the last 50 lines of the detection log
tail -50 logs/detect.log

# Search for errors across all logs
grep ERROR logs/*.log
```

### 5.3 Email Alerts

When running in continuous mode, the pipeline sends an email alert if a day fails after all retries and the pipeline stops. Configure via environment variables (see Section 3.3).

### 5.4 Pipeline Status File

The orchestrator writes `output/pipeline_status.json` with real-time state:
- Pipeline status (running, completed, failed, waiting, stopped)
- Per-step status, timing, and return codes
- Continuous mode metadata: current day, days completed, day timing history

---

## 6. Troubleshooting

### FDSNWS Timeouts / Connection Failures

**Symptoms**: `Connection failed`, `FDSNException`, or `Cannot connect to FDSNWS` errors in `logs/ingest.log`.

**Causes**: Raspberry Shake FDSNWS server downtime, network issues, rate limiting.

**Resolution**:
1. Check server status: `curl -s https://data.raspberryshake.org/fdsnws/dataselect/1/version`
2. Increase `max_retries` and `retry_base_delay_seconds` in `1-ingestion/config.yaml`
3. Reduce `max_workers` to lower concurrent requests (default: 2)
4. Increase `station_delay_seconds` to add more delay between requests
5. In continuous mode, the pipeline will retry automatically (up to 5 times per day)

**Note**: Do NOT use the `"RASPISHAKE"` shortcut in ObsPy. Use the full URL `https://data.raspberryshake.org`.

### GPU Out-of-Memory (OOM)

**Symptoms**: `CUDA out of memory` or `RuntimeError` during PhaseNet detection.

**Resolution**:
1. Reduce `classify_batch_size` in `3-detection/config.yaml` (default: 256; try 64 or 32)
2. Ensure no other GPU processes are running (`nvidia-smi`)
3. As a fallback, PhaseNet will use CPU if CUDA is unavailable (slower but functional)

### Empty Data / No Picks

**Symptoms**: `No data available` for many chunks; daily CSV has 0 picks; `No events found`.

**Causes**: Station temporarily offline, data not yet available (latency), incorrect time range.

**Resolution**:
1. Check the data completeness panel in the dashboard for station gaps
2. Verify the station is active at `https://stationview.raspberryshake.org/`
3. Increase `data_latency_hours` if data is consistently delayed
4. Check station health via the dashboard's station map (color-coded by recent data presence)

### Processing Failures

**Symptoms**: `Cannot read metadata`, `Processing failed`, or `No metadata file` in `logs/process.log`.

**Resolution**:
1. Ensure ingestion ran successfully first (metadata files should exist under `output/1-metadata/`)
2. Check if the StationXML file is corrupted: try re-running ingestion with `--force` is not supported for step 1, so delete the stale XML from `output/1-metadata/` and re-run ingestion
3. Verify the station's response information is available from FDSNWS

### Merge Conflicts in Catalog

**Symptoms**: Duplicate events or missing days in `output/5-catalog/catalog.csv`.

**Resolution**:
1. Run `python 5-catalog/catalog.py --config 5-catalog/config.yaml --rebuild` to regenerate the catalog from scratch
2. The rebuild reads all daily files from `output/4-events/` and creates a fresh catalog

### Pipeline Stops in Continuous Mode

**Symptoms**: Email alert received, `pipeline_status.json` shows `"status": "failed"`.

**Resolution**:
1. Check the log file for the failed step (shown in the status file)
2. Fix the underlying issue (server down, disk full, corrupt data)
3. Restart: `python run_pipeline.py --continuous` (auto-resumes from the failed day)

### Disk Space Issues

**Symptoms**: `OSError: [Errno 28] No space left on device` or pipeline silently fails.

**Resolution**:
1. Check disk usage via the dashboard's disk panel or `du -sh output/*`
2. The largest consumers are `output/1-raw/` and `output/2-processed/`
3. To free space, archive or remove old raw data: `rm -rf output/1-raw/2025/`
4. Processed data can be regenerated from raw data, so it can be deleted and re-created with `--force`
5. Set up log rotation (already configured: 10 MB per file, 5 backups)

---

## 7. Data Management

### Output Directory Structure

```
output/
  1-raw/{year}/{jday}/         Raw miniSEED waveforms
    AM.R0F2D.00.EHZ.2026.029.mseed
  1-metadata/                  Cached StationXML files
    AM.R0F2D.xml
  1-downloads.db               SQLite download tracker
  2-processed/{year}/{jday}/   Response-removed, filtered miniSEED
  3-picks/{year}/{jday}/       Daily pick CSVs
    2026.029.picks.csv
  4-events/{year}/{jday}/      Daily event + assignment CSVs
    2026.029.events.csv
    2026.029.assignments.csv
  5-catalog/
    catalog.csv                Master event catalog (all days)
    assignments.csv            All pick assignments with global event IDs
  pipeline_status.json         Live pipeline status
```

### Disk Usage Estimates (11 stations)

| Directory | Typical Size/Day | Notes |
|-----------|-----------------|-------|
| `1-raw/` | 20-40 MB | 3 channels per RS3D, 4+ per RS4D |
| `1-metadata/` | ~1 MB total | Cached, refreshed weekly |
| `2-processed/` | 20-40 MB | Same structure as raw |
| `3-picks/` | <100 KB | Single CSV per day |
| `4-events/` | <100 KB | Two CSVs per day |
| `5-catalog/` | <10 MB total | Grows slowly over time |

**Approximate total**: ~60-80 MB per day, or ~22-30 GB per year.

### Data Retention

There is no built-in retention policy. Recommended approaches:

1. **Archive raw data**: After processing, copy `output/1-raw/` to external storage and delete local copies. Processed data can be regenerated from raw data.
2. **Keep processed data**: `output/2-processed/` is needed to re-run detection without re-processing. Keep it if disk allows.
3. **Keep picks and events**: Small footprint; keep indefinitely.
4. **Catalog**: Small and cumulative; keep indefinitely.

### Backup

The minimum backup set for full reproducibility is:
- `stations.json` (station definitions)
- `output/5-catalog/catalog.csv` (final results)
- `output/5-catalog/assignments.csv` (pick assignments)
- All `config.yaml` files (processing parameters)

For full data recovery, also back up `output/1-raw/` (everything else can be regenerated).
