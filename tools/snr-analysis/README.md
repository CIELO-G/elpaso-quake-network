# SNR Analysis — El Paso Seismic Network

Station sensitivity and signal recovery analysis across the network.

## Quick Start

```bash
cd snr-analysis/

# Step 1: Select events (interactive or auto)
python fetch_events.py --auto         # auto-pick best candidates
python fetch_events.py                # interactive selection

# Step 2: Compute SNR for all stations × events
python compute_snr.py

# Step 3: Generate figures and summary
python plot_results.py
python plot_results.py --skip-map     # if cartopy not installed
```

## Output

- `output/data/selected_events.json` — chosen events for each scenario
- `output/data/snr_results.csv` — per-station SNR measurements
- `output/data/snr_summary.csv` — station recommendations
- `output/figures/` — PNG + PDF figures
