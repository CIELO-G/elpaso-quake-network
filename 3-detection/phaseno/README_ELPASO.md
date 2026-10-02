# PhaseNO in the El Paso pipeline

Upstream: <https://github.com/sun-hongyu/PhaseNO> v1.0.1 (MIT, Hongyu Sun).
Sun, Ross, Zhu & Azizzadenesheli (2023), *Phase Neural Operator for
Multi-Station Picking of Seismic Arrivals*, arXiv:2305.03269.

## What is upstream (do not edit)

| file | note |
|---|---|
| `phaseno_model.py` | model classes extracted **verbatim** from `phaseno_predict.ipynb` so they can be imported; no functional changes |
| `models/epoch=19-step=1140000.ckpt` | pretrained weights (NorCal earthquakes + STEAD noise), 38 MB |
| `phaseno_predict.ipynb`, `phaseno_plot.ipynb` | original notebooks — reference for the inference algorithm |
| `env.yml`, `LICENSE`, `README.md`, `phaseno.png` | upstream as shipped |
| `example/` | Ridgecrest demo; `example/waveforms` (284 MB) is gitignored |

## What is ours

| file | purpose |
|---|---|
| `predict.py` | production picker (see below); writes the pipeline's picks CSV schema (`model=PhaseNO:v1.0.1`) |
| `phaseno_fast.py` | tiny device-aware loader used by the benchmarks |
| `bench_profile.py` | per-layer timing (91% of runtime is the graph layers) |
| `../../tests/test_phaseno_predict.py` | unit tests for the plumbing (run in the main env; model tests need the `phaseno` env) |

## Numerics vs the notebook (independently verified)

`predict.py` reproduces the notebook's inference: the same causal 1-Hz
highpass (obspy `Trace.filter('highpass')` = 4-pole Butterworth,
`sosfilt`), per-window z-score / 10, coordinate normalisation, edge list,
forward, sigmoid, overlap averaging and the vendored `_detect_peaks`.
An equivalence run on a real hour (2026-06-08 21:00–22:00, 13 stations)
gave interior probability differences ≤ 2e-6 (float32 round-trip) and
identical picks. The only deliberate deviation is at the last 30 s of a
chunk (full window ending at the chunk end vs the notebook's zero-padded
partial window) — discarded by the padding except for the final chunk of
a day. Two review findings that motivated this rigor: a zero-phase filter
changed ~1/3 of picks, and feeding RS4D accelerometer channels as
horizontals changed those stations' probabilities by up to 0.9.

Upstream quirks the adapter deliberately does not reproduce: the
notebook's `Stream.slide` windows are 3001 samples and stored at
`index*step`, making notebook pick times 10 ms early; and with
sub-sample-offset station grids (this network) `np.array(windowed_st)`
raises on modern numpy — the unmodified notebook cannot run on this data.

## Production behaviour

Gaps: masked samples and runs of exact zeros ≥ 1 s (ingest zero-fill) are
treated as gaps; picks within 1 s of a gap edge are dropped; stations
with < 50 % valid samples in a chunk leave the graph for that chunk.
`2-processed` has gaps *interpolated* by `process.py` (unrecoverable
here) — use `1-raw` with `--input-units counts` when that matters.
Chunks are 1 h with 40-s padding (a multiple of the 20-s window step, so
every chunk shares one window grid); the peak-spacing rule is re-applied
across seams. Coordinates use one fixed centre (mean of all
`stations.json` entries). Failure = no CSV + exit 1; empty CSV = ran and
found nothing. `--force` overwrites an existing output.
Amplitudes are measured on 3-Hz-highpassed velocity (pipeline
convention, comparable to PhaseNet's), picks on the 1-Hz-filtered data.

## Environment

    conda create -n phaseno python=3.11 -y
    conda run -n phaseno pip install torch torch_geometric pytorch_lightning obspy pandas scipy tqdm

Modern stack (torch 2.14, PyG 2.8, Lightning 2.6) loads the 2023
checkpoint with Lightning's auto-upgrade; upstream `env.yml` pins CUDA
10.2 and is not used on the Mac. MPS output matches CPU to ~1e-6.

## Run

    conda run -n phaseno python 3-detection/phaseno/predict.py \
        --day-dir output/2-processed/2026/159 --stations stations.json \
        --out output/3-picks-phaseno/2026/159/2026.159.picks.csv

`--input-units counts` for `1-raw` input (amplitudes then divided by the
instrument sensitivity from `stations.json`). Defaults: thresholds 0.4
(pipeline convention; upstream 0.3), `--dis-range-km 150` (full graph —
the upstream default 30 km would fragment this 6–114 km network),
`--highpass 1.0` (upstream), device auto → MPS.

## Performance (M-series Mac, 13 stations)

~0.37 s per 30-s network window on MPS (2.3× CPU), ≈27 min per day.
Window batching, memory-layout tweaks and fp16 were all measured and do
not help; the cost is intrinsic to the edge MLPs.
