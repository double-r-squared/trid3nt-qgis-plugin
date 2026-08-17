#!/usr/bin/env python3
"""Q2 NUMERIC-FIDELITY A/B (close-out). Element-wise comparison of the
2025-computed Muncie subgrid property tables (ComputeMuncie.cs output over the
REAL terrain) vs the shipped 6.x GUI-computed tables (extract_muncie_mesh.py
npz). Cells matched by center (bit-identical bijection), faces matched by
midpoint. The 6.x GUI and the 2025 beta are DIFFERENT codebases, so bit-identity
is NOT the bar -- hydraulic equivalence (small, monotone curve diffs) is.

Curves have DIFFERENT breakpoint counts between the two codepaths (different
filter/histogram sampling), so every comparison samples BOTH curves on a shared
elevation grid over their overlap and diffs the interpolated values.

Usage: ab_compare.py <2025_out_dir> <muncie_mesh.npz> <out_dir> [label]
"""
import sys, json, os
import numpy as np
from scipy.spatial import cKDTree
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT2025 = sys.argv[1]
NPZ     = sys.argv[2]
OUTDIR  = sys.argv[3]
LABEL   = sys.argv[4] if len(sys.argv) > 4 else "terrain"
os.makedirs(OUTDIR, exist_ok=True)

d = np.load(NPZ)
NREAL = 5391

# ---- shipped 6.x ground truth ----
cc6   = np.asarray(d["cells_center"])[:NREAL]
cvi6  = np.asarray(d["cell_vol_info"])[:NREAL]        # (Ncell,2) start,count
cvv6  = np.asarray(d["cell_vol_values"], np.float64)  # (M,2) [elev, vol]
fai6  = np.asarray(d["face_area_info"])                # (Nface,2)
fav6  = np.asarray(d["face_area_values"], np.float64)  # (K,4) [elev,area,wp,mann]
fp_idx6 = np.asarray(d["faces_facepoint_idx"])         # (Nface,2)
fp_xy6  = np.asarray(d["facepoints_coord"], np.float64) # (Nfp,2)
fmid6 = 0.5*(fp_xy6[fp_idx6[:,0]] + fp_xy6[fp_idx6[:,1]])

# ---- 2025 dumped ----
def rf64(n): return np.fromfile(f"{OUT2025}/{n}", dtype="<f8")
def rf32(n): return np.fromfile(f"{OUT2025}/{n}", dtype="<f4").astype(np.float64)
def ri32(n): return np.fromfile(f"{OUT2025}/{n}", dtype="<i4")
cc25   = rf64("regen_cell_centers.f64").reshape(-1,2)
cinfo  = ri32("cell_info.i32").reshape(-1,2)
celev  = rf32("cell_elev.f32"); cvol = rf32("cell_vol.f32")
fmid25 = rf64("regen_face_midpoints.f64").reshape(-1,2)
finfo  = ri32("face_info.i32").reshape(-1,2)
felev  = rf32("face_elev.f32"); farea = rf32("face_area.f32")
fwp    = rf32("face_wp.f32");   fmann = rf32("face_mann.f32")

# ---- match cells by center (bijection) ----
dcell, icell = cKDTree(cc25).query(cc6, k=1)   # for each 6.x cell -> nearest 2025 cell
cell_match_ok = dcell < 1.0
print(f"[cell match] {cell_match_ok.sum()}/{NREAL} within 1ft  max_disp={dcell.max():.4f}ft "
      f"unique2025={len(np.unique(icell[cell_match_ok]))}")

# ---- match faces by midpoint ----
dface, iface = cKDTree(fmid25).query(fmid6, k=1)
face_match_ok = dface < 1.0
print(f"[face match] {face_match_ok.sum()}/{len(fmid6)} within 1ft  max_disp={dface.max():.4f}ft "
      f"(6.x faces={len(fmid6)} 2025 faces={len(fmid25)})")


def curve_ship_cell(i):
    s,c = cvi6[i]; seg = cvv6[s:s+c]
    return seg[:,0], seg[:,1]            # elev, vol

def curve_2025_cell(j):
    s,c = cinfo[j]
    return celev[s:s+c], cvol[s:s+c]

def curve_ship_face(i):
    s,c = fai6[i]; seg = fav6[s:s+c]
    return seg[:,0], seg[:,1], seg[:,2], seg[:,3]   # elev, area, wp, mann

def curve_2025_face(j):
    s,c = finfo[j]
    return felev[s:s+c], farea[s:s+c], fwp[s:s+c], fmann[s:s+c]


def sample_pair(xa, ya, xb, yb, npts=25):
    """Interp both monotone curves on shared x-overlap; return (grid, ya_i, yb_i)."""
    if len(xa) < 2 or len(xb) < 2: return None
    lo = max(xa[0], xb[0]); hi = min(xa[-1], xb[-1])
    if hi <= lo: return None
    g = np.linspace(lo, hi, npts)
    return g, np.interp(g, xa, ya), np.interp(g, xb, yb)


# ================= CELL VOLUME-ELEVATION =================
cell_absdiff=[]; cell_reldiff=[]; cell_ship=[]; cell_2025=[]
cell_minel_ship=[]; cell_minel_2025=[]
depth_probe = {1.0:[],3.0:[],5.0:[]}   # abs vol diff at fixed depths above cell min
for i in range(NREAL):
    if not cell_match_ok[i]: continue
    j = icell[i]
    es,vs = curve_ship_cell(i); er,vr = curve_2025_cell(j)
    cell_minel_ship.append(es[0]); cell_minel_2025.append(er[0])
    sp = sample_pair(es,vs,er,vr)
    if sp is None: continue
    g,a,b = sp
    cell_absdiff.append(np.abs(b-a)); cell_ship.append(a); cell_2025.append(b)
    vmax = max(a.max(), 1e-6)
    cell_reldiff.append(np.abs(b-a)/vmax)
    for dep in depth_probe:
        stage = es[0]+dep
        if stage<=min(es[-1],er[-1]) and stage>=max(es[0],er[0]):
            depth_probe[dep].append(abs(np.interp(stage,er,vr)-np.interp(stage,es,vs)))

cell_absdiff=np.concatenate(cell_absdiff); cell_reldiff=np.concatenate(cell_reldiff)
cell_ship_f=np.concatenate(cell_ship); cell_2025_f=np.concatenate(cell_2025)
cell_minel_ship=np.array(cell_minel_ship); cell_minel_2025=np.array(cell_minel_2025)
cell_corr=np.corrcoef(cell_ship_f, cell_2025_f)[0,1]

# ================= FACE AREA / WETTED-PERIM / MANNING =================
def face_col(colidx):
    """colidx: 1=area,2=wp,3=mann. Returns (absdiff, ship_vals, 2025_vals)."""
    ad=[]; sv=[]; rv=[]
    for i in range(len(fmid6)):
        if not face_match_ok[i]: continue
        j=iface[i]
        cs=curve_ship_face(i); cr=curve_2025_face(j)
        sp=sample_pair(cs[0],cs[colidx], cr[0],cr[colidx])
        if sp is None: continue
        g,a,b=sp; ad.append(np.abs(b-a)); sv.append(a); rv.append(b)
    return np.concatenate(ad), np.concatenate(sv), np.concatenate(rv)

fa_ad,fa_s,fa_r = face_col(1)   # area
fwp_ad,fwp_s,fwp_r = face_col(2) # wetted perimeter
fm_ad,fm_s,fm_r = face_col(3)   # manning

# face min elevation A/B
fmin_s=[]; fmin_r=[]
for i in range(len(fmid6)):
    if not face_match_ok[i]: continue
    j=iface[i]; fmin_s.append(fav6[fai6[i][0],0]); fmin_r.append(felev[finfo[j][0]])
fmin_s=np.array(fmin_s); fmin_r=np.array(fmin_r)

def stats(name, ad, s=None, r=None):
    o={"col":name,"max_abs":float(ad.max()),"mean_abs":float(ad.mean()),
       "p50_abs":float(np.percentile(ad,50)),"p95_abs":float(np.percentile(ad,95)),
       "p99_abs":float(np.percentile(ad,99)),"n":int(ad.size)}
    if s is not None:
        o["corr"]=float(np.corrcoef(s,r)[0,1])
        # relative error only where the 6.x value is hydraulically meaningful
        # (>5% of column max) -- near-zero bottoms make raw rel error meaningless
        floor=0.05*float(np.abs(s).max()); mrel=np.abs(s)>floor
        o["mean_rel_pct_above_5pct_max"]=float(np.mean(np.abs(r[mrel]-s[mrel])/np.abs(s[mrel]))*100) if mrel.any() else None
        o["ship_range"]=[float(s.min()),float(s.max())]
    return o

summary={
 "label":LABEL,
 "cells_matched":int(cell_match_ok.sum()),"faces_matched":int(face_match_ok.sum()),
 "faces_unmatched":int((~face_match_ok).sum()),
 "cell_center_max_disp_ft":float(dcell.max()),
 "face_mid_max_disp_matched_ft":float(dface[face_match_ok].max()),
 "cell_min_elev": {"max_abs_ft":float(np.abs(cell_minel_2025-cell_minel_ship).max()),
                   "mean_abs_ft":float(np.abs(cell_minel_2025-cell_minel_ship).mean())},
 "face_min_elev": {"max_abs_ft":float(np.abs(fmin_r-fmin_s).max()),
                   "mean_abs_ft":float(np.abs(fmin_r-fmin_s).mean())},
 "cell_volume": {**stats("cell_volume_cf",cell_absdiff,cell_ship_f,cell_2025_f),
                 "abs_vol_diff_at_depth_ft":{str(k):(float(np.mean(v)) if v else None) for k,v in depth_probe.items()},
                 "p95_at_depth_ft":{str(k):(float(np.percentile(v,95)) if v else None) for k,v in depth_probe.items()}},
 "face_area": stats("face_area_sqft",fa_ad,fa_s,fa_r),
 "face_wetted_perim": stats("face_wp_ft",fwp_ad,fwp_s,fwp_r),
 "face_manning": stats("face_mann",fm_ad,fm_s,fm_r),
}
with open(f"{OUTDIR}/ab_stats_{LABEL}.json","w") as fh: json.dump(summary,fh,indent=2)
print(json.dumps(summary,indent=2))

# ================= PLOT =================
fig,ax=plt.subplots(2,3,figsize=(16,9))
fig.suptitle(f"Q2 numeric fidelity: 2025-computed vs 6.x GUI subgrid tables (Muncie, {LABEL} terrain)",fontweight="bold")

def hist(a,arr,title,unit,logy=True):
    a.hist(arr,bins=80,color="#3b7dd8",edgecolor="none")
    a.set_title(title,fontsize=10); a.set_xlabel(unit); a.set_ylabel("count")
    if logy: a.set_yscale("log")
    a.grid(alpha=0.3)

hist(ax[0,0],cell_absdiff,f"Cell volume abs diff (corr={cell_corr:.5f})","abs |V_2025 - V_6x| (cf)")
hist(ax[0,1],fa_ad,f"Face area abs diff (corr={summary['face_area']['corr']:.5f})","abs diff (sq ft)")
hist(ax[0,2],fwp_ad,f"Face wetted-perim abs diff (corr={summary['face_wetted_perim']['corr']:.5f})","abs diff (ft)")
hist(ax[1,0],(cell_minel_2025-cell_minel_ship),"Cell min-elevation diff","2025 - 6.x (ft)",logy=False)
hist(ax[1,1],(fmin_r-fmin_s),"Face min-elevation diff","2025 - 6.x (ft)",logy=False)
# scatter: cell volume parity
ax[1,2].scatter(cell_ship_f, cell_2025_f, s=1, alpha=0.15, color="#2a9d5c")
lim=[0, np.percentile(cell_ship_f,99.5)]
ax[1,2].plot(lim,lim,"r--",lw=1); ax[1,2].set_xlim(lim); ax[1,2].set_ylim(lim)
ax[1,2].set_title(f"Cell volume parity (corr={cell_corr:.5f}, p95 abs={summary['cell_volume']['p95_abs']:.1f} cf)",fontsize=10)
ax[1,2].set_xlabel("6.x GUI volume (cf)"); ax[1,2].set_ylabel("2025 volume (cf)"); ax[1,2].grid(alpha=0.3)
plt.tight_layout()
plotp=f"{OUTDIR}/q2_divergence_{LABEL}.png"
plt.savefig(plotp,dpi=110); print(f"[plot] {plotp}")
