# Pipeline scripts

Utilities that complement the 5-stage pipeline. Run from the project root with the `elpaso-quake` conda env active.

| Script | Purpose |
|---|---|
| `build_templates.py` | Build EQcorrscan template library from confirmed quarry blasts |
| `run_template_match.py` | Matched-filter detection — scan continuous data for events resembling templates |
| `build_quarries_geojson.py` | Refresh the MSHA + OSM quarry overlay used by the dashboard |
| `nlloc_build_grids.py` | (Re)build NLLoc velocity + travel-time grids — needed after touching `lib/location/nlloc_config.py` or `stations.json` |
| `backup.py` | Snapshot the catalog, assignments, and downloads DB to a backup dir |

## Template matching workflow

Matched-filter detection cross-correlates known events (templates) against continuous data to find similar events that the main detection pipeline missed. Especially effective for **repeating quarry blasts** from the same source.

### 1. Build the template library

```bash
python scripts/build_templates.py
```

Reads all confirmed `event_type=quarry_blast` events from the catalog, fetches the processed waveforms around each pick, and writes an EQcorrscan `Tribe` archive plus a manifest:

```
output/templates/blast_templates.tgz   ← Tribe archive (binary)
output/templates/manifest.json         ← human-readable template list
```

Default template params (override with flags):
- 1 s before pick + 5 s after pick = 6 s template length
- Bandpass 2–20 Hz
- Resampled to 50 Hz
- Templates with fewer than 4 full-length channels are dropped

```bash
# Use earthquake events instead of blasts
python scripts/build_templates.py --event-type earthquake

# Longer windows for regional events
python scripts/build_templates.py --before 2 --after 10

# Different filter band (e.g., for low-frequency content)
python scripts/build_templates.py --filter 1 10 --samp-rate 20
```

Re-run after confirming new blasts to grow the library.

### 2. Run detection over a date range

```bash
python scripts/run_template_match.py --start 2026-04-01 --end 2026-04-15
```

Scans each day's processed waveforms with all templates, appends matches to a CSV:

```
output/templates/detections.csv
```

Columns: `detect_time, template_id, correlation, threshold, n_chans, day`.

**Important note on correlation values**: `correlation` is the **sum** across all channels of the template (so max possible = `n_chans`). To get the per-channel average correlation (0–1, more interpretable), divide by `n_chans`:

```python
import pandas as pd
df = pd.read_csv("output/templates/detections.csv")
df["per_chan_corr"] = df["correlation"].abs() / df["n_chans"]
df[df["per_chan_corr"] >= 0.5]  # high-confidence detections
```

### 3. Tuning detection sensitivity

The detection threshold is **MAD-based** by default (`8 × median absolute deviation` of the correlation time series — adaptively chooses based on noise floor for each template-day). Higher = fewer / more confident detections.

| Setting | Meaning |
|---|---|
| `--threshold 8` (default) | Permissive — many candidates, more noise |
| `--threshold 10` | Balanced — recommended for first pass |
| `--threshold 12+` | Conservative — only strong matches |
| `--threshold-type absolute --threshold 0.6` | Use raw correlation; ~0.6 = strong match across channels |

```bash
# More conservative pass over the full back-fill
python scripts/run_template_match.py --start 2025-10-22 --end 2026-06-07 \
    --threshold 12 --fresh
```

`--fresh` wipes the previous `detections.csv` so you start clean.

### 4. Interpreting results

A single real event will typically:
- Be detected by **multiple templates simultaneously** (within ±5 s)
- Have **per-channel correlation ≥ 0.5** in the strongest match
- Be the right time of day for a blast (06:00–18:00 local for most quarries)
- Sit at a plausible location (cross-reference with the quarry overlay in the dashboard)

A useful one-liner to cluster simultaneous detections into events:

```python
import csv
from obspy import UTCDateTime
rows = sorted(csv.DictReader(open("output/templates/detections.csv")),
              key=lambda r: r["detect_time"])
events = []
i = 0
while i < len(rows):
    t0 = UTCDateTime(rows[i]["detect_time"])
    group = [rows[i]]
    j = i + 1
    while j < len(rows) and UTCDateTime(rows[j]["detect_time"]) - t0 <= 5:
        group.append(rows[j])
        j += 1
    events.append(group)
    i = j
print(f"{len(events)} candidate events from {len(rows)} raw detections")
```

### Performance

On a 10-core Mac with 33 templates × 14 stations × 50 Hz:

| Date range | Wall time |
|---|---|
| 1 day | ~55 s |
| 7 days | ~6 min |
| 30 days | ~30 min |
| Full back-fill (~200 days) | ~3 hours |

Use `--cores 8` to push past the default of 4 if you have spare cores.
