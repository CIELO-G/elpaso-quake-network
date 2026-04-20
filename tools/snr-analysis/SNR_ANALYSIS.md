# SNR Analysis of the El Paso Seismic Network

## Overview

This document describes the signal-to-noise ratio (SNR) analysis performed on the El Paso community seismic network (CIELO-G). The goal is to evaluate station sensitivity and signal recovery for regional earthquakes originating in the West Texas / Permian Basin induced seismicity zone, and to characterize performance differences between stations installed on bedrock versus basin sediments.

**Figures produced for the poster:**
- `fig1_snr_vs_distance` — SNR vs. epicentral distance (P and S phases)
- `fig4_frequency_bands` — Frequency-dependent SNR per station
- `fig_waveform_examples` — Cherry-picked waveform panels at three magnitudes

---

## Network

The analysis uses 9 stations from the El Paso deployment. Two remote stations in the Las Cruces area (R5912, R9070) were excluded as they are outside the primary study area.

| Station | Network | Instrument | Site Type |
|---------|---------|-----------|-----------|
| R4B41   | AM      | RS3D      | Bedrock   |
| RE2E7   | AM      | RS3D      | Bedrock   |
| RADA1   | AM      | RS3D      | Bedrock   |
| KIDD    | EP      | Broadband | Bedrock   |
| R0F2D   | AM      | RS3D      | Basin     |
| R3E95   | AM      | RS3D      | Basin     |
| RDB14   | AM      | RS3D      | Basin     |
| S50BE   | AM      | RS3D      | Basin     |
| RC9B8   | AM      | RS3D      | Basin     |

- RS3D: Raspberry Shake 3-component geophone, 100 Hz sample rate, 5 Hz corner frequency
- Broadband (KIDD): EP network broadband seismometer, 100 Hz, 0.05 Hz corner frequency
- All waveforms are instrument-response-removed and output in velocity (m/s)

---

## Event Selection

We queried the USGS ComCat catalog (via ObsPy FDSN client) for regional earthquakes satisfying:

- **Magnitude:** M2.5+
- **Distance:** < 300 km from the network center (31.85N, 106.40W)
- **Time period:** 2025-09-01 to 2026-04-01 (the network's operational period)

This returned **100 events**, predominantly induced seismicity from the Delaware Basin / Permian Basin region east of El Paso.

- Magnitude range: M2.5 -- M3.9
- Distance range: 149 -- 302 km (epicentral)
- Composition: 65 events M2.5--3.0, 35 events M3.0--3.9

These events were selected because they represent the primary regional seismicity source that the El Paso network should be able to monitor. The Permian Basin is one of the most active induced seismicity zones in the United States.

---

## Methodology

### Data Source

All waveforms were sourced from the pipeline's existing processed archive (`output/2-processed/`). These waveforms have already been:
1. Downloaded from FDSNWS (Raspberry Shake data server for AM stations, IRIS for EP.KIDD)
2. Instrument-response removed (output = velocity, m/s)
3. Detrended and tapered

No additional data downloads were required -- the pipeline's continuous ingestion had already acquired data for all 100 event days.

### Theoretical Arrival Times

P-wave and S-wave arrival times were computed using ObsPy's TauP module with the **iasp91** global velocity model. For each event-station pair:

- P arrivals: computed using phase list [P, p, Pn, Pg], selecting the first arrival
- S arrivals: computed using phase list [S, s, Sn, Sg], selecting the first arrival

The iasp91 model produces theoretical arrivals that are approximately 1 second early relative to observed arrivals for this region. This is expected because iasp91 is a global average and does not account for the thick sedimentary basin structure of the Permian Basin and Rio Grande Rift. This offset is small relative to the SNR measurement windows and does not significantly affect the results.

### SNR Computation

For each event-station pair, SNR was computed on the **vertical component** (EHZ for Raspberry Shake, HHZ for broadband) using the following windows:

**Noise window:**
- Duration: 30 seconds
- Position: ending 5 seconds before the predicted P arrival
- This window samples ambient background noise before any earthquake signal arrives

**P-phase signal window:**
- Start: predicted P arrival
- End: predicted S arrival
- This captures the full P-wave coda including direct P, Pn, Pg, and P-coda

**S-phase signal window:**
- Start: predicted S arrival
- End: 10 seconds after predicted S arrival
- This captures the direct S-wave and early S-coda

**Calculation:**
```
SNR_linear = RMS(signal_window) / RMS(noise_window)
SNR_dB = 20 * log10(SNR_linear)
```

Where RMS is the root-mean-square amplitude of the waveform in the given window.

SNR was computed in four frequency bands by applying a zero-phase Butterworth bandpass filter before windowing:
- **Broadband:** 1 -- 45 Hz
- **Low:** 1 -- 5 Hz
- **Mid:** 5 -- 15 Hz
- **High:** 15 -- 40 Hz

### Detection Thresholds

Two thresholds are used to classify signal quality:
- **Detection threshold:** SNR = 3 (9.5 dB) -- signal is distinguishable from noise
- **Good quality threshold:** SNR = 10 (20 dB) -- signal is well above noise, suitable for analysis

---

## Results

### Overall Performance

| Metric | Value |
|--------|-------|
| Total measurements | 900 (100 events x 9 stations) |
| Measurements with data | 893 (99%) |
| Bedrock mean P-phase SNR | 2.5 dB |
| Bedrock mean S-phase SNR | 5.6 dB |
| Basin mean P-phase SNR | 0.7 dB |
| Basin mean S-phase SNR | 1.8 dB |
| Bedrock--basin S-phase difference | +3.8 dB |

S-phase SNR is consistently higher than P-phase across all stations. This is expected because S-waves carry more energy than P-waves for shallow crustal earthquakes at regional distances.

### Per-Station Results (broadband 1--45 Hz)

| Station | Type | P mean (dB) | P median (dB) | S mean (dB) | S median (dB) | n |
|---------|------|-------------|----------------|-------------|----------------|---|
| R4B41   | Bedrock | 6.6 | 6.6 | 12.1 | 12.1 | 100 |
| KIDD    | Bedrock | 2.9 | 2.3 | 5.4  | 4.1  | 98  |
| RE2E7   | Bedrock | 1.2 | 1.5 | 4.1  | 3.8  | 100 |
| RADA1   | Bedrock | -0.8 | -0.2 | 0.7 | 0.9 | 100 |
| RC9B8   | Basin | 3.4 | 3.0 | 5.0  | 4.3  | 100 |
| RDB14   | Basin | 0.4 | 0.2 | 1.8  | 1.1  | 100 |
| S50BE   | Basin | 0.2 | 0.5 | 1.6  | 1.6  | 100 |
| R3E95   | Basin | 0.2 | 0.1 | 1.4  | 1.7  | 100 |
| R0F2D   | Basin | -0.8 | -0.1 | -0.8 | -0.1 | 95  |

**Key observations:**

- **R4B41** is the top performer with S-phase SNR of 12.1 dB mean -- the only station that consistently exceeds the detection threshold. It is a bedrock site near the Franklin Mountains with low ambient noise.
- **RC9B8** is the best-performing basin station (S = 5.0 dB), outperforming two of the bedrock stations (RADA1 and RE2E7 in P-phase). This may reflect a particularly quiet installation site or favorable local geology.
- **KIDD** (the broadband reference station) performs well (S = 5.4 dB) despite being classified as a basin site in earlier analyses. Its reclassification to bedrock reflects its location on consolidated material near the Franklin Mountains.
- **R0F2D** is the weakest station with negative mean SNR in both phases, meaning the regional signals are typically below the ambient noise level at this site.
- **RADA1** underperforms for a bedrock site (P = -0.8 dB, S = 0.7 dB), suggesting either a noisy installation environment or site conditions that are not truly bedrock.

### Detection Rates

Detection rate = percentage of the 100 events where S-phase SNR exceeded the 9.5 dB detection threshold.

| Station | Type | Detection Rate | Good Quality Rate (>20 dB) |
|---------|------|----------------|---------------------------|
| R4B41   | Bedrock | 63% | 24% |
| RC9B8   | Basin   | 30% | 6%  |
| KIDD    | Bedrock | 28% | 5%  |
| RE2E7   | Bedrock | 20% | 4%  |
| RDB14   | Basin   | 15% | 3%  |
| R3E95   | Basin   | 12% | 1%  |
| S50BE   | Basin   | 11% | 2%  |
| R0F2D   | Basin   | 11% | 2%  |
| RADA1   | Bedrock | 9%  | 2%  |

Only R4B41 detects a majority of the regional events. Most stations detect fewer than 30% of M2.5+ events at 150--300 km. This is consistent with the limitations of 5 Hz geophone sensors at regional distances where much of the earthquake energy is below 5 Hz.

### Frequency-Dependent SNR

**(Corresponds to Figure 4 — Frequency Band Comparison)**

| Station | Type | Low (1--5 Hz) S | Mid (5--15 Hz) S | High (15--40 Hz) S |
|---------|------|-----------------|-------------------|---------------------|
| R4B41   | Bedrock | 8.9  | 12.2 | 2.6 |
| KIDD    | Bedrock | 5.2  | 6.1  | 1.6 |
| RE2E7   | Bedrock | 2.3  | 5.4  | 0.8 |
| RADA1   | Bedrock | -5.2 | 1.5  | 0.2 |
| RC9B8   | Basin   | -0.5 | 9.2  | 1.6 |
| RDB14   | Basin   | -0.4 | 3.8  | 0.2 |
| S50BE   | Basin   | 2.1  | 3.2  | -0.1 |
| R3E95   | Basin   | -1.7 | 2.1  | -0.2 |
| R0F2D   | Basin   | -1.1 | 0.9  | -0.5 |

**Key observations:**

- **Mid-band (5--15 Hz) dominates** the signal for all stations. This is expected: the Raspberry Shake geophones have a 5 Hz corner frequency, so they are most sensitive in this range. Regional earthquake energy from M2.5--3.9 events at 150--300 km peaks in the 2--10 Hz band.
- **Low-band (1--5 Hz)** is where bedrock stations have the largest advantage. R4B41 achieves 8.9 dB while most basin stations are negative. KIDD (broadband, 0.05 Hz corner) also performs well here (5.2 dB) because its instrument has flat response down to 0.05 Hz.
- **High-band (15--40 Hz)** shows uniformly poor SNR across all stations (< 3 dB). High-frequency energy attenuates rapidly over regional distances, so this is expected regardless of site type.
- **RC9B8 is anomalous** for a basin station: its mid-band SNR (9.2 dB) is the second highest in the network, exceeding most bedrock stations. This suggests the site may have unusually low cultural noise or favorable shallow geology.
- **Basin sediments attenuate low-frequency signal**: basin stations are systematically negative in the 1--5 Hz band. This is consistent with basin amplification effects that increase noise at low frequencies while the geophones cannot fully capture the low-frequency earthquake signal.

### SNR vs. Distance Trend

**(Corresponds to Figure 1 — SNR vs. Epicentral Distance)**

The SNR vs. distance plot shows binned median trends with interquartile ranges for bedrock and basin groups. Key features:

- Both groups show the expected decrease in SNR with distance, consistent with geometric spreading and anelastic attenuation.
- The bedrock group median is consistently 2--4 dB above the basin group median at all distances.
- The separation is most pronounced in the S-phase, where bedrock stations maintain positive SNR out to ~250 km while basin stations drop below zero around 200 km.
- Individual station lines reveal that R4B41 is a clear outlier above the bedrock group trend, and R0F2D is consistently at the bottom of the basin group.

### Waveform Examples

**(Corresponds to Figure — Waveform Examples)**

Three events were cherry-picked to illustrate the network's response across magnitudes:

**Panel 1: M3.9, 194 km (2026-03-07T07:11:26 UTC)**
- Strongest event in the dataset. Clear P and S arrivals visible on all 9 stations.
- R4B41 shows the largest amplitudes. Basin stations show attenuated but still visible arrivals.
- Bedrock mean S-phase SNR: 33.2 dB. Basin mean: 18.2 dB. Gap: 15.0 dB.

**Panel 2: M3.1, 194 km (2026-02-16T07:30:43 UTC)**
- Moderate event at the same distance as Panel 1. Signal visible on most stations but weaker.
- Bedrock mean S-phase SNR: 15.9 dB. Basin mean: 13.3 dB. Gap: 2.6 dB.
- The smaller bedrock--basin gap at this magnitude suggests the site effect is more pronounced for larger events with more low-frequency energy.

**Panel 3: M2.7, 190 km (2026-03-30T15:50:11 UTC)**
- Near the detection limit. Signal is marginal on most stations.
- S-phase SNR averages only 2.0 dB -- most stations cannot reliably distinguish this event from noise.
- Only R4B41 shows a clear arrival. This illustrates the practical detection limit of the network for regional events.

Waveforms are displayed filtered at 3--45 Hz with predicted P (red dashed) and S (blue dashed) arrival times from the iasp91 velocity model. Theoretical arrivals are approximately 1 second early relative to observed arrivals due to the difference between the global average model and regional crustal structure.

---

## Code Structure

All analysis code lives in `tools/snr-analysis/`. The pipeline is:

```
fetch_batch.py → compute_batch.py → plot_batch.py / plot_waveforms.py
```

### `config.yaml`
Central configuration file. Contains:
- Paths to pipeline data (processed waveforms, catalog, stations)
- FDSN data source URLs
- SNR window parameters (noise duration, gap, signal extension)
- Frequency band definitions
- Detection thresholds
- Site type classification
- Station exclusion list
- Output format settings

### `utils.py`
Shared helper functions:
- `load_config()` — loads YAML config, resolves relative paths
- `load_stations()` — parses stations.json
- `station_distance_azimuth()` — computes distance/azimuth from event to station using ObsPy `gps2dist_azimuth`
- `get_site_type()` — maps station name to site classification
- `compute_rms()` — root-mean-square of a numpy array
- `snr_ratio()` / `snr_db()` — linear and dB SNR conversion

### `fetch_batch.py`
Queries USGS ComCat for regional events.

**How it works:**
1. Connects to the USGS FDSN event service via ObsPy
2. Searches for events within the configured radius, magnitude, and time range
3. Computes epicentral distance from each event to the network center
4. Filters by maximum distance (default 300 km)
5. Deduplicates by origin time
6. Saves the event list to `output/data/batch_events.json`

**Usage:**
```bash
python fetch_batch.py --count 100 --min-mag 2.5 --max-dist 300
```

### `compute_batch.py`
Core SNR engine. Processes all events across all stations.

**How it works:**
1. Loads the event list from `batch_events.json` and station definitions from `stations.json`
2. For each event-station pair:
   a. Computes epicentral distance and azimuth
   b. Computes theoretical P and S travel times using TauP (iasp91)
   c. Defines the analysis window: from 30s before P to 10s after S
   d. Loads the waveform from the pipeline's processed archive (`output/2-processed/{year}/{jday}/`)
   e. Selects the vertical component (EHZ or HHZ)
   f. For each frequency band, applies a bandpass filter and measures RMS in the noise and signal windows
   g. Computes SNR for P-phase, S-phase, and combined windows separately
3. Saves a checkpoint CSV after each event (supports `--resume`)
4. Outputs `batch_snr_results.csv` with all measurements

**Key features:**
- Waveform cache (`output/cache/`) for FDSN-fetched data (not needed when using processed data)
- Rate limiting for FDSN requests (`--delay` flag)
- Resume support (`--resume` skips already-computed event-station pairs)
- `--skip-fdsn` flag to only use existing processed data (no downloads)

**Output columns:**
`event_id, origin_time, event_mag, event_dist_km, station, network, model, site_type, distance_km, azimuth_deg, p_travel_s, s_travel_s, data_source, snr_p_broadband, snr_db_p_broadband, snr_s_broadband, snr_db_s_broadband, snr_combined_broadband, snr_db_combined_broadband, [same for low/mid/high bands], noise_rms, signal_rms_p, signal_rms_s`

### `plot_batch.py`
Generates the publication figures.

**Figure 1 (`fig1_snr_vs_distance`):**
- Two panels: P-phase (left), S-phase (right)
- Each station plotted as a thin trend line (binned median over distance)
- Bold line for the bedrock and basin group medians
- Shaded interquartile range behind group lines
- Station names labeled at the right edge of each line
- Detection threshold marked at 9.5 dB

**Figure 4 (`fig4_frequency_bands`):**
- Four panels: broadband, low, mid, high frequency bands
- Horizontal bars per station showing mean SNR
- Light bars = P-phase, dark bars = S-phase
- Stations grouped by site type (bedrock, then basin) with separators
- Station labels colored by site type

### `plot_waveforms.py`
Generates the cherry-picked waveform example figure.

**Three events selected to show the range of network response:**
1. M3.9 at 194 km — clear signal on all stations
2. M3.1 at 194 km — moderate, visible on most stations
3. M2.7 at 190 km — near the detection limit

**How it works:**
1. For each event, loads processed waveforms for all 9 stations
2. Computes TauP P and S arrival times
3. Filters waveforms at 3--45 Hz
4. Stacks traces vertically, sorted by epicentral distance
5. Normalizes each trace independently for visual comparison
6. Marks P (red dashed) and S (blue dashed) predicted arrivals
7. Labels each trace with station name, distance, and P/S SNR in dB

---

## Reproducing the Analysis

```bash
cd tools/snr-analysis/

# 1. Fetch 100 M2.5+ events within 300 km
python fetch_batch.py --count 100 --min-mag 2.5 --max-dist 300

# 2. Compute SNR using existing processed data (no downloads, ~8 minutes)
python compute_batch.py --skip-fdsn

# 3. Generate figures
python plot_batch.py
python plot_waveforms.py
```

Output files:
- `output/data/batch_events.json` — selected events
- `output/data/batch_snr_results.csv` — all SNR measurements (900 rows)
- `output/figures/fig1_snr_vs_distance.{png,pdf}`
- `output/figures/fig2_station_boxplots.{png,pdf}`
- `output/figures/fig4_frequency_bands.{png,pdf}`
- `output/figures/fig_waveform_examples.{png,pdf}`

---

## Dependencies

- Python 3.10+
- ObsPy (waveform I/O, FDSN client, TauP)
- NumPy, Pandas (computation)
- Matplotlib (plotting)
- PyYAML (config)
- Cartopy (optional, for maps)
