#!/usr/bin/env python3
"""Fidelity of the 2025-regenerated Muncie mesh vs the shipped 6.x mesh
(ADR-transplant, terrain-independent). Reads the MeshGen harness output
(regen_centers*.f64) and the 6.x plan HDF; reports cell/face counts + the
cell-center displacement distribution."""
import sys
import numpy as np
from scipy.spatial import cKDTree
import h5py

HDF = sys.argv[1] if len(sys.argv) > 1 else \
    "services/workers/hecras/fixtures/muncie_smoke/wrk_source/Muncie.p04.tmp.hdf"
IN = sys.argv[2] if len(sys.argv) > 2 else \
    "services/workers/hecras2025/subst/crux/transplant/meshgen_in"

f = h5py.File(HDF, "r")
g = f["/Geometry/2D Flow Areas"]
cc6 = np.asarray(g["2D Interior Area/Cells Center Coordinate"][()])[:5391]
nface6 = int(g["2D Interior Area/Faces FacePoint Indexes"].shape[0])
nfp6 = int(g["2D Interior Area/FacePoints Coordinate"].shape[0])

regen = np.fromfile(f"{IN}/regen_centers.f64").reshape(-1, 2)
d, idx = cKDTree(cc6).query(regen, k=1)
print("=== 2025-REGENERATED vs 6.x MUNCIE MESH ===")
print(f"real cells:  regen={len(regen)}  6.x=5391   ({'EXACT' if len(regen)==5391 else 'DIFF'})")
print(f"faces:       regen=11166        6.x={nface6} (delta {11166-nface6})")
print(f"facepoints:  regen=5776         6.x={nfp6} (delta {5776-nfp6})")
print(f"cell-center displacement (ft): max={d.max():.6f} mean={d.mean():.6f} p95={np.percentile(d,95):.6f}")
print(f"bijection: {len(np.unique(idx))}/{len(cc6)} unique 6.x cells matched")
import os
if os.path.exists(f"{IN}/regen_centers_virt.f64"):
    rv = np.fromfile(f"{IN}/regen_centers_virt.f64").reshape(-1, 2)
    print(f"with virtual cells: regen={len(rv)}  6.x total=5765   ({'EXACT' if len(rv)==5765 else 'DIFF'})")
