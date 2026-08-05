#!/usr/bin/env python3
"""Depth + mesh-wireframe proof render of a solved HEC-RAS plan HDF."""
import sys
from pathlib import Path
import h5py
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

PLAN = Path(sys.argv[1])
OUT = Path(sys.argv[2])
AREA = "2D Interior Area"
A = f"/Geometry/2D Flow Areas/{AREA}"
_FILL = 1e30

with h5py.File(PLAN, "r") as f:
    g = f[A]
    fp_idx = g["Cells FacePoint Indexes"][()]
    fp_xy = g["FacePoints Coordinate"][()]
    minel = g["Cells Minimum Elevation"][()].astype(np.float64)
    base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"
    mw = f[f"{base}/2D Flow Areas/{AREA}/Maximum Water Surface"][()]
    a = np.where(np.abs(mw) > _FILL, np.nan, mw).astype(np.float64)
    if a.ndim == 2:
        a = a[0] if a.shape[0] < a.shape[1] else a[:, 0]

n = min(fp_idx.shape[0], a.shape[-1], minel.shape[0])
depth = a[:n] - np.where(np.abs(minel[:n]) > _FILL, np.nan, minel[:n])
polys, dvals = [], []
for c in range(n):
    idxs = [int(i) for i in fp_idx[c] if int(i) != -1]
    if len(idxs) < 3:
        continue
    polys.append(fp_xy[idxs])
    dvals.append(depth[c] if depth[c] > 0.01 else np.nan)

fig, ax = plt.subplots(figsize=(11, 8))
pc = PolyCollection(polys, array=np.array(dvals), cmap="Blues",
                    edgecolors="0.55", linewidths=0.15)
pc.set_clim(0, np.nanmax(dvals))
ax.add_collection(pc)
ax.autoscale_view()
ax.set_aspect("equal")
ax.set_title("Muncie -- writer-authored geometry (ADR 0132 OI-2) solved on 6.x\n"
             f"peak depth (mesh wireframe overlaid), {len(polys)} cells", fontsize=11)
cb = fig.colorbar(pc, ax=ax, shrink=0.7)
cb.set_label("peak depth (ft)")
ax.set_xlabel("Easting (ft, NAD83 SP Indiana E)")
ax.set_ylabel("Northing (ft)")
fig.tight_layout()
fig.savefig(OUT, dpi=110)
print(f"wrote {OUT}  wet_cells={int(np.nansum(np.array(dvals) > 0.01))}  "
      f"max_depth={np.nanmax(dvals):.2f} ft")
