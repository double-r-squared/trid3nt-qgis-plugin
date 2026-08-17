#!/usr/bin/env python3
"""FAITHFUL TRANSPLANT author (close-out, step 4). Write the
2025-computed Muncie subgrid property tables (ComputeMuncie.cs, REAL terrain)
into a COPY of the 6.x Muncie geometry -- mapped into the 6.x cell/face ordering
via the bit-identical center bijection (cells) and midpoint match (faces) -- then
the deck is solved by the production 6.x RasGeomPreprocess + RasUnsteady
(transplant_solve.py mode=prebuilt) and compared to the baseline.

This is the exact-topology in-place transplant OI-2 describes: the 2025 VALUES
replace the GUI-computed values in Muncie's own 6.x topology. Real cells (0..5390)
get 2025 curves; virtual/ghost cells (5391..5764) and the handful of unmatched
boundary faces keep their original 6.x curves. Ragged Info/Values arrays are
rebuilt (2025 breakpoint counts differ from the GUI). Pure h5py; no server code.

Usage: build_faithful_transplant.py <2025_out_dir> <src_wrk_dir> <dst_wrk_dir> <npz>
"""
import sys, shutil
from pathlib import Path
import h5py, numpy as np
from scipy.spatial import cKDTree

O    = sys.argv[1]
SRC  = Path(sys.argv[2])
DST  = Path(sys.argv[3])
NPZ  = sys.argv[4]
PLAN = "Muncie.p04.tmp.hdf"
A    = "/Geometry/2D Flow Areas/2D Interior Area"
NREAL = 5391

d = np.load(NPZ)
cc6 = np.asarray(d["cells_center"])[:NREAL]
fp_idx6 = np.asarray(d["faces_facepoint_idx"]); fp_xy6 = np.asarray(d["facepoints_coord"], np.float64)
fmid6 = 0.5*(fp_xy6[fp_idx6[:,0]] + fp_xy6[fp_idx6[:,1]])

def rf64(n): return np.fromfile(f"{O}/{n}", dtype="<f8")
def rf32(n): return np.fromfile(f"{O}/{n}", dtype="<f4").astype(np.float32)
def ri32(n): return np.fromfile(f"{O}/{n}", dtype="<i4")
cc25   = rf64("regen_cell_centers.f64").reshape(-1,2)
cinfo  = ri32("cell_info.i32").reshape(-1,2); celev=rf32("cell_elev.f32"); cvol=rf32("cell_vol.f32")
fmid25 = rf64("regen_face_midpoints.f64").reshape(-1,2)
finfo  = ri32("face_info.i32").reshape(-1,2)
felev  = rf32("face_elev.f32"); farea=rf32("face_area.f32"); fwp=rf32("face_wp.f32"); fmann=rf32("face_mann.f32")

# cell bijection (6.x real cell i -> 2025 cell), face match (6.x face i -> 2025 face or -1)
_, icell = cKDTree(cc25).query(cc6, k=1)
dface, iface = cKDTree(fmid25).query(fmid6, k=1)
fmatch = np.where(dface < 1.0, iface, -1)
print(f"[author] cells matched {NREAL}; faces matched {(fmatch>=0).sum()}/{len(fmid6)} "
      f"({(fmatch<0).sum()} kept-original)")

DST.mkdir(parents=True, exist_ok=True)
for fp in SRC.glob("*.*"): shutil.copy2(fp, DST / fp.name)

with h5py.File(DST / PLAN, "r+") as f:
    g = f[A]
    NCELL = g["Cells Volume Elevation Info"].shape[0]     # 5765 (incl virtual)
    NFACE = g["Faces Area Elevation Info"].shape[0]        # 11164
    # ---- originals (for virtual cells + unmatched faces) ----
    cvi_o = g["Cells Volume Elevation Info"][()]; cvv_o = g["Cells Volume Elevation Values"][()]
    fai_o = g["Faces Area Elevation Info"][()];   fav_o = g["Faces Area Elevation Values"][()]
    cmin_o = g["Cells Minimum Elevation"][()];    fmin_o = g["Faces Minimum Elevation"][()]

    # ---- rebuild CELL volume-elevation ----
    cvi = np.zeros((NCELL,2), np.int32); cvv=[]; cmin=cmin_o.copy(); idx=0
    for i in range(NCELL):
        if i < NREAL:
            j = icell[i]; s,c = cinfo[j]
            seg = np.column_stack([celev[s:s+c], cvol[s:s+c]]).astype(np.float32)
            cmin[i] = celev[s]
        else:
            s,c = cvi_o[i]; seg = cvv_o[s:s+c].astype(np.float32)
        cvi[i] = (idx, seg.shape[0]); cvv.append(seg); idx += seg.shape[0]
    cvv = np.concatenate(cvv, 0)

    # ---- rebuild FACE area-elevation (Z, Area, WettedPerim, Manning) ----
    fai = np.zeros((NFACE,2), np.int32); fav=[]; fmin=fmin_o.copy(); idx=0
    for i in range(NFACE):
        j = fmatch[i]
        if j >= 0:
            s,c = finfo[j]
            seg = np.column_stack([felev[s:s+c], farea[s:s+c], fwp[s:s+c], fmann[s:s+c]]).astype(np.float32)
            fmin[i] = felev[s]
        else:
            s,c = fai_o[i]; seg = fav_o[s:s+c].astype(np.float32)
        fai[i] = (idx, seg.shape[0]); fav.append(seg); idx += seg.shape[0]
    fav = np.concatenate(fav, 0)

    def replace(name, arr):
        attrs = dict(g[name].attrs); del g[name]
        ds = g.create_dataset(name, data=arr)
        for k,v in attrs.items(): ds.attrs[k]=v
    replace("Cells Volume Elevation Info", cvi)
    replace("Cells Volume Elevation Values", cvv)
    replace("Faces Area Elevation Info", fai)
    replace("Faces Area Elevation Values", fav)
    g["Cells Minimum Elevation"][...] = cmin
    g["Faces Minimum Elevation"][...] = fmin
    print(f"[author] wrote 2025 tables: cell Values {cvv_o.shape[0]}->{cvv.shape[0]} rows, "
          f"face Values {fav_o.shape[0]}->{fav.shape[0]} rows")
print(f"[author] faithful deck -> {DST}")
