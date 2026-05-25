# NonLinLoc integration

This directory holds the NonLinLoc-specific configuration that the
dashboard's `/api/event/{id}/relocate` endpoint uses to produce
publication-grade event locations.

## Layout

```
nlloc/
├── README.md                  (this file)
└── control_template.in        (optional hand-edits; generated control
                               files override anything not here)

output/nlloc/                  (generated; gitignored)
├── grids/
│   ├── elpaso.P.mod.buf       (velocity model — P)
│   ├── elpaso.S.mod.buf       (velocity model — S)
│   └── time/
│       ├── elpaso.P.{STA}.time.buf   (travel-time grid per station per phase)
│       └── elpaso.S.{STA}.time.buf
└── runs/                      (per-event temp dirs created by the wrapper)
```

## Setup

NonLinLoc is **not** on PATH by default. The wrapper looks for binaries via
the `NLLOC_BIN_DIR` env var, falling back to a sibling clone at
`../NonLinLoc/src/bin/`. Build from source once:

```bash
git clone https://github.com/alomax/NonLinLoc.git ../NonLinLoc
cd ../NonLinLoc/src && cmake . && make -j
```

(There's no conda-forge or homebrew package as of this writing.)

## One-time grid generation

After install, build the per-station travel-time grids once. This reads
`stations.json` + `DEFAULT_WEST_TEXAS_MODEL` and runs Vel2Grid +
Grid2Time. Re-run whenever `stations.json` changes (e.g. new stations).

```bash
python scripts/nlloc_build_grids.py
```

The output lands in `output/nlloc/grids/` (gitignored). Expect ~15 MB per
station per phase (travel-time + takeoff-angle grid pair at 1 km spacing)
— ~190 MB total for 13 stations × 2 phases. Drop the angle grids by
editing `GTMODE … ANGLES_NO` in `scripts/nlloc_build_grids.py` if you
want to halve that.

## Velocity model

The active model is `DEFAULT_WEST_TEXAS_MODEL` from
`lib/location/velocity_model.py` — a six-layer Permian Basin / Rio Grande
Rift approximation. Vp/Vs ratio ~1.75 throughout, Gardner-rule densities.

To swap in a published reference (TexNet / Savvaidis / Frohlich / etc.),
edit `lib/location/velocity_model.py` and re-run the grid generation
script. The rendered NLLoc fragments in `lib/location/nlloc_config.py`
will pick up the change automatically.
