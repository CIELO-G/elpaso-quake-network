# Changelog

All notable changes to the El Paso seismic processing pipeline are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Five-stage automated seismic processing pipeline (ingestion, processing, detection, association, catalog)
- Pipeline orchestrator (`run_pipeline.py`) with single-run and continuous modes
- Continuous mode with auto-resume, retry logic (5 retries per day), and email alerts on persistent failure
- FDSNWS data ingestion with 1-hour chunking, exponential-backoff retry, and SQLite download tracking
- Waveform preprocessing: instrument response removal (pre-filter 0.5-1.0-40.0-45.0 Hz), detrending, tapering
- PhaseNet phase detection via SeisBench with configurable probability thresholds and amplitude measurement
- Three-component channel support for RS3D, RS4D (with accelerometer fallback), and broadband stations
- GaMMA Bayesian phase association (BGMM method) with DBSCAN pre-clustering
- Local magnitude (ML) computation using Hutton and Boore (1987) attenuation relation
- Post-association quality control: minimum station count filter
- Incremental event catalog with globally unique event IDs (`ep{YYYYMMDD}-{NNNN}`)
- FastAPI dashboard with Leaflet map, event visualization, and real-time pipeline monitoring
- Dashboard API: 14 endpoints for status, catalog, statistics, progress, throughput, errors, disk usage, station health, event rate, data completeness, pick quality, station picks, and log viewing
- Station health monitoring (data presence in last 3 day directories)
- Per-step and per-day throughput tracking with ETA computation
- Rotating file logging (10 MB per file, 5 backups) for all pipeline stages
- Shared library (`lib/`) for configuration loading, SQLite tracking, and logging setup
- Station list (`stations.json`) supporting RS3D, RS4D, and broadband instrument types
- Conda environment specification (`environment.yml`) with all dependencies
- Comprehensive documentation: operator guide, scientific methods, API reference, architecture decision records
- CONTRIBUTING.md with code style, branch naming, PR process, and testing requirements

### Changed
- Velocity model updated to Vp=6.0, Vs=3.47 km/s (from Vp=5.5, Vs=3.18) based on published Rio Grande Rift upper-crustal velocities
- Pre-filter corners updated to [0.5, 1.0, 40.0, 45.0] Hz (from [0.5, 1.0, 45.0, 49.0]) to avoid noise amplification near Nyquist
- Removed redundant bandpass filter after instrument response removal to prevent double-filtering at band edges
- DBSCAN epsilon reduced to 25 s (from 50 s) to prevent merging of separate events
- Wood-Anderson static magnification updated to 2800 (from 2080) per Uhrhammer and Collins (1990) revision
- ML minimum hypocentral distance raised to 10 km (from 1 km) to stay within Hutton and Boore (1987) calibration range
- Association search bounds expanded to +/- 0.7 degrees (from 0.5) to cover full network extent with margin
- Minimum picks per event raised to 6 (from 3) to match minimum station requirement (3 stations x 2 phases)
- Association pick probability filter raised to 0.5 (from 0.4) for two-tier filtering strategy

### Fixed
- Date boundary handling: midnight end times correctly map to the previous calendar day
- FDSNWS rate limiting: configurable inter-station delay and max concurrent workers
- SMTP email alerts use SMTP_SSL on port 465 for reliable delivery
- Pipeline stops on persistent failure instead of silently skipping days

### Security
- No secrets stored in configuration files; SMTP credentials read from environment variables only
