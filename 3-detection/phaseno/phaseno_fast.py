"""Device-aware loader for the UNMODIFIED upstream PhaseNO model.

Kept deliberately tiny: no patches to the model. (A vectorised rewrite of
the graph layer's per-edge loop was benchmarked at 0 gain — the cost is
the edge MLPs themselves — so the upstream code is used as-is.)

Apple MPS output was verified identical to CPU to ~1e-6 and runs ~2.3x
faster (~0.37 s per 30-s network window, ~27 min per 13-station day).
"""

from __future__ import annotations

from pathlib import Path

import torch

from phaseno_model import PhaseNO


def pick_device(prefer: str = "auto") -> torch.device:
    if prefer == "cpu":
        return torch.device("cpu")
    if prefer in ("auto", "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    if prefer in ("auto", "cuda") and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def load_model(ckpt: str | Path, device: str = "auto") -> PhaseNO:
    model = PhaseNO.load_from_checkpoint(str(ckpt), map_location="cpu")
    model.eval()
    return model.to(pick_device(device))
