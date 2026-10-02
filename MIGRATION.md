# Moving the El Paso monitor to a new Mac (Mac Studio M2 + RAID)

Same architecture as the current machine (arm64), so the three conda
environments and the MPS GPU path carry over unchanged. The RAID becomes
the home of `output/` (the 545 GB+ data tree); everything else is code
from GitHub plus a handful of files git deliberately does not carry.

Do the steps in this order. Nothing here is destructive to the old
machine until step 1 is complete and verified.

## 0. Before touching the new machine (old Mac)

1. **Commit and push** everything pending (`git status` must be clean,
   `git log origin/main..HEAD` empty). GitHub is the code transport.
2. Plug in the archive drive and run **both** backups:
       ./backup_workspace.sh
       elpaso-quake-network/backup_to_drive.sh
   This also carries the gitignored assets listed in step 3.
3. Export the hand-built envs (already in the repo as of 2026-10-02):
   `3-detection/phasenet/environment-phasenet.yml`,
   `3-detection/phaseno/environment-phaseno.yml`, plus the main
   `environment.yml`.
4. **Stop the pipeline** (quit El Paso Monitor) before the data copy so
   two machines never process the same days.

## 1. New Mac — code and environments

    mkdir -p ~/Research/Elpaso && cd ~/Research/Elpaso
    git clone https://github.com/CIELO-G/elpaso-quake-network.git
    git clone https://github.com/CIELO-G/elpaso-seismic.git        # public site
    cd elpaso-quake-network
    conda env create -f environment.yml
    conda env create -f 3-detection/phasenet/environment-phasenet.yml
    conda env create -f 3-detection/phaseno/environment-phaseno.yml

NonLinLoc: clone/copy `~/Research/Elpaso/NonLinLoc` (pristine upstream)
and `make` it; set `NLLOC_BIN_DIR` if the binaries land elsewhere.

## 2. Data on the RAID

The code resolves data as `<repo>/output` (dashboard/deps.py,
lib/monitoring.py, every stage config) — so put the tree on the RAID and
**symlink** it; nothing in the code changes:

    rsync -avh --progress /Volumes/<archive-drive>/Data/Research/Elpaso/elpaso-quake-network/output/ \
        /Volumes/<RAID>/elpaso/output/
    ln -s /Volumes/<RAID>/elpaso/output ~/Research/Elpaso/elpaso-quake-network/output

With RAID capacity, **stop pruning raw**: simply never run
`scripts/prune_raw.py`; raw stays local and the archive drive becomes a
true backup instead of the only copy. (RAID redundancy is not a backup —
keep running `backup_to_drive.sh` to the external drive, and keep the
code on GitHub.)

Growth planning: ~1.7 GB/day (0.5 raw + 1.2 processed) ≈ 600 GB/yr
with the current 14 stations.

## 3. Files git does not carry (copy from the old Mac / the drive)

| what | where | note |
|---|---|---|
| PhaseNet weights | `3-detection/model/190703-214543/` | gitignored |
| PhaseNO checkpoint | `3-detection/phaseno/models/*.ckpt` | 38 MB, commit or copy |
| Dashboard password + CARTO key | `deploy/app/local.env` | **not on the drive by design** — copy by hand, `chmod 600` |
| NLLoc travel-time grids | `output/nlloc/` (3 GB) | comes with output/, or regenerate: `scripts/nlloc_build_grids.py` |
| Station responses | `stations/`, `output/1-metadata/` | on the drive |
| Download tracker | `output/1-downloads.db` | comes with output/ |

## 4. App, remote access, publishing

    ./deploy/app/build_app.sh          # bakes new paths + local.env into the .app
    tailscale up                       # join the tailnet
    tailscale serve --bg 8000          # TLS front door

Rename the new machine to `el-paso-monitor` in the Tailscale admin console
(after removing the old one) so `https://el-paso-monitor.<tailnet>.ts.net`
keeps working on your laptop. `gh auth login` on the new Mac so the
daily public-site push (`scripts/export_public_site.py --push`) works.

**Make it reboot-proof this time:** add El Paso Monitor.app to Login
Items and enable "Start up automatically after a power failure"
(System Settings → Energy). The pipeline runs inside the app process.

## 5. First run

Launch the app. The pipeline resumes from the last completed day in
`output/` — it will backfill the days missed during the move. Check:
`/api/health`, the station-health panel, and that the queue chip loads.

## 6. Dissertation figures (separate repo, optional)

13 `make.py` scripts under `dissertation-figures/chapter4` hard-code
`~/Research/Elpaso/...`. If the username differs, fix
once with sed, or export `ELPASO_PIPELINE_ROOT` where supported.

## Known machine-specific state to re-create

- `deploy/app/local.env` (see table), Tailscale identity, `gh` login,
  the archive drive mount point `/Volumes/<archive-drive>` (in `backup_to_drive.sh`
  and `scripts/prune_raw.py`), Login Items.
