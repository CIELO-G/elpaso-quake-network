"""Where does the time go? Per-layer timing of one forward pass."""
import sys, time
from pathlib import Path
import torch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import phaseno_fast, phaseno_model as pm

CKPT = Path(__file__).resolve().parent / "models" / "epoch=19-step=1140000.ckpt"
N, NT = 13, 3000
torch.manual_seed(0)
X = torch.randn(N, 5, NT) * 0.1
coords = torch.rand(N, 2); X[:, 3] = coords[:, 0:1]; X[:, 4] = coords[:, 1:2]
rows = [[], [], [], [], [], []]
for i in range(N):
    for j in range(N):
        for k, v in enumerate([i, j, coords[i,0], coords[i,1], coords[j,0], coords[j,1]]):
            rows[k].append(float(v))
edge = torch.tensor(rows, dtype=torch.float)

for dev in ["cpu", "mps"]:
    model = phaseno_fast.load_model(CKPT, dev)
    times = {}
    def wrap(name, mod):
        orig = mod.forward
        def f(*a, **k):
            if dev == "mps": torch.mps.synchronize()
            t0 = time.perf_counter(); out = orig(*a, **k)
            if dev == "mps": torch.mps.synchronize()
            times[name] = times.get(name, 0) + time.perf_counter() - t0
            return out
        mod.forward = f
    for n in ["fno0","gno0","fno1","gno1","fno2","gno3","fno4","gno4","fno5","gno5","fno6","fno7"]:
        wrap(n, getattr(model, n))
    x, e = X.to(dev), edge.to(dev)
    with torch.no_grad():
        model.forward((x, None, e))          # warm-up
        times.clear()
        t0 = time.perf_counter()
        for _ in range(3): model.forward((x, None, e))
        if dev == "mps": torch.mps.synchronize()
        tot = (time.perf_counter() - t0) / 3
    fno = sum(v for k, v in times.items() if k.startswith("fno")) / 3
    gno = sum(v for k, v in times.items() if k.startswith("gno")) / 3
    print(f"{dev}: total {tot*1000:.0f} ms | FNO layers {fno*1000:.0f} ms | GNO layers {gno*1000:.0f} ms | "
          + ", ".join(f"{k}={v/3*1000:.0f}" for k, v in times.items()))
