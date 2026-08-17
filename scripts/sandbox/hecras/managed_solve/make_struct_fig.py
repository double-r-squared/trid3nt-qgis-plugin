#!/usr/bin/env python3
"""2D structure-authoring A/B seam-probe figure (baseline vs authored weir).

Renders the solver's per-cell depth field as FILLED cell footprints on the
structured mesh grid (pcolormesh) -- never cell-center scatter. The StructChannel
deck is 60x300 m, 10 m cells (6x30). Recomputed live from the result HDFs so the
messaging (bit-identical, crest faces unchanged) is not a stale hard-coded claim.

Re-derives its own numbers each run: no re-solve needed, struct_base_result.h5 /
struct_weir_result.h5 already exist under the probe host dir.
"""
import h5py
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

P = "/home/nate/hecras_probe2025"
DEP = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh/Cell Depth"

CELL = 10.0
NX, NY = 6, 30
x_edges = np.arange(NX + 1) * CELL
y_edges = np.arange(NY + 1) * CELL


def load(case):
    with h5py.File(f"{P}/{case}_result.h5", "r") as f:
        xy = f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
        depth = f[DEP][-1, :]
        fd = f["/Geometry/2D Flow Areas/Base Mesh/Face Data"][:]
        fme = f["/Geometry/2D Flow Areas/Base Mesh/Property Tables/Face Minimum Elevation"][:]
    return xy, depth, fd, fme


def to_grid(xy, d):
    col = np.clip((xy[:, 0] / CELL).astype(int), 0, NX - 1)
    row = np.clip((xy[:, 1] / CELL).astype(int), 0, NY - 1)
    g = np.full((NY, NX), np.nan)
    g[row, col] = d
    return g


def crest_face_minelev(xy, fd, fme, crest_y=150.0):
    """Min-elevation of the faces straddling the weir centerline y=150 (n should
    equal the mesh width in cells, 6)."""
    c1, c2 = fd[:, 0], fd[:, 1]
    n = xy.shape[0]
    valid = (c1 >= 0) & (c1 < n) & (c2 >= 0) & (c2 < n)
    y1, y2 = xy[c1[valid], 1], xy[c2[valid], 1]
    cross = np.abs(np.abs(y1 - y2) - CELL) < 1e-3
    lo, hi = np.minimum(y1, y2), np.maximum(y1, y2)
    on_crest = cross & (lo < crest_y) & (hi > crest_y)
    idxs = np.where(valid)[0][on_crest]
    return fme[idxs]


xy_a, depth_a, fd_a, fme_a = load("struct_base")
xy_b, depth_b, fd_b, fme_b = load("struct_weir")

crest_a = crest_face_minelev(xy_a, fd_a, fme_a)
crest_b = crest_face_minelev(xy_b, fd_b, fme_b)

grid_a = to_grid(xy_a, depth_a)
grid_b = to_grid(xy_b, depth_b)
grid_diff = grid_b - grid_a
max_abs_diff = float(np.nanmax(np.abs(grid_diff)))
vmax = float(max(np.nanmax(grid_a), np.nanmax(grid_b)))

fig = plt.figure(figsize=(13, 5.6))
gs = GridSpec(1, 3, width_ratios=[1, 1, 1], wspace=0.32,
              left=0.055, right=0.97, top=0.78, bottom=0.14)

status = "HYDRAULICALLY INERT" if max_abs_diff < 1e-6 else "MEASURABLY LIVE"
fig.suptitle(
    f"HEC-RAS 2025 engine: authored 2D weir is {status}\n"
    f"weir centerline at y=150 (red dashed); max|B-A| depth = {max_abs_diff:.6f} m; "
    f"crest-line faces (n={len(crest_a)}) min-elev: base={crest_a.mean():.2f} "
    f"weir={crest_b.mean():.2f} m",
    fontsize=11.5, y=0.97)

ax0 = fig.add_subplot(gs[0, 0])
ax1 = fig.add_subplot(gs[0, 1])
ax2 = fig.add_subplot(gs[0, 2])

for ax, grid, title in [
    (ax0, grid_a, "A: baseline (no structure)"),
    (ax1, grid_b, "B: authored weir, crest=2.0 m"),
]:
    im = ax.pcolormesh(x_edges, y_edges, np.ma.masked_invalid(grid), cmap="viridis",
                        vmin=0, vmax=vmax, shading="flat", edgecolors="white", linewidth=0.3)
    ax.axhline(150, color="red", ls="--", lw=1.6, zorder=5)
    ax.set_title(title, fontsize=10.5)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)  [inflow at top, outlet at bottom]")
    cb = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.03)
    cb.set_label("final depth (m)")

dmax = float(np.nanmax(np.abs(grid_diff))) or 1.0
im2 = ax2.pcolormesh(x_edges, y_edges, np.ma.masked_invalid(grid_diff), cmap="RdBu_r",
                      vmin=-dmax if dmax > 1e-6 else -1, vmax=dmax if dmax > 1e-6 else 1,
                      shading="flat", edgecolors="white", linewidth=0.3)
ax2.axhline(150, color="black", ls="--", lw=1.6, zorder=5)
ax2.set_title("B - A  (weir effect)", fontsize=10.5)
ax2.set_xlabel("x (m)")
cb2 = fig.colorbar(im2, ax=ax2, fraction=0.045, pad=0.03)
cb2.set_label("depth diff (m)")

fig.text(0.5, 0.02,
          "StructureLayer -> engine Weir bridge ABSENT in this beta "
          "(InitializeComputeDriver wires ONLY InitializeDriver_Culverts); "
          "cells rendered as FILLED footprints on the 10 m structured mesh, never cell-center scatter.",
          ha="center", fontsize=7.6, style="italic")

out = "/home/nate/Documents/trid3nt-local/docs/proof/templates/hecras_structure_2d_seam_probe_ab.png"
fig.savefig(out, dpi=120, bbox_inches="tight")
print("wrote", out, "max_abs_diff", max_abs_diff, "crest_a", crest_a, "crest_b", crest_b)
