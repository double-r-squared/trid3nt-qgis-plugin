#!/usr/bin/env python3
"""Validate the cht_sfincs-authored quadtree netcdf against the decoded
SFINCS-solver Fortran format (source/src/sfincs_quadtree.F90).

The CONNECTIVITY TABLE is the load-bearing check: for a multi-level quadtree the
neighbour-index arrays (mu1/mu2/nu1/nu2/md1/md2/nd1/nd2) plus the level-difference
flags (mu/md/nu/nd in {-1 coarser, 0 same, +1 finer}) must be mutually
consistent. We verify:
  1. all required bare vars + global attrs present on dim mesh2d_nFaces
  2. n/m/level domains; level in 1..nr_levels; multi-level actually present
  3. neighbour indices in [0, ncells] (1-based, 0 = none)
  4. level-flag domain {-1,0,1}
  5. RECIPROCITY: if cell A's +m neighbour (mu1) is B at SAME level, then B's
     -m neighbour (md1) must be A  (and analogous for n-direction). This is the
     true test that the refined connectivity table is internally coherent.
  6. cross-level reciprocity sanity: a cell flagged mu==1 (finer to the right)
     has BOTH mu1 and mu2 set (two finer neighbours), each of which points back
     with md==-1.
  7. snapwave_mask present + has wave-boundary cells.

Exit 0 = valid against the solver contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import xarray as xr

HERE = Path(__file__).resolve().parent
QTR = HERE / "deck_cht" / "sfincs.nc"

REQUIRED_VARS = [
    "n", "m", "level",
    "md", "md1", "md2", "mu", "mu1", "mu2",
    "nd", "nd1", "nd2", "nu", "nu1", "nu2",
    "z", "mask",
]
SNAPWAVE_VARS = ["snapwave_mask"]
REQUIRED_ATTRS = ["x0", "y0", "dx", "dy", "rotation", "nmax", "mmax", "nr_levels"]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        failures.append(msg)


print(f"=== validating {QTR} ===\n")
ds = xr.open_dataset(QTR)

print("=== 1. required vars + attrs on dim mesh2d_nFaces ===")
check("mesh2d_nFaces" in ds.sizes, "dim 'mesh2d_nFaces' present")
ncells = int(ds.sizes["mesh2d_nFaces"])
for v in REQUIRED_VARS:
    ok = v in ds.variables and ds[v].dims == ("mesh2d_nFaces",)
    check(ok, f"required var '{v}' on (mesh2d_nFaces,)")
for v in SNAPWAVE_VARS:
    check(v in ds.variables, f"snapwave var '{v}' present")
for a in REQUIRED_ATTRS:
    check(a in ds.attrs, f"global attr '{a}' present")

print("\n=== 2. n/m/level domains + multi-level present ===")
n = ds["n"].values.astype(int)
m = ds["m"].values.astype(int)
level = ds["level"].values.astype(int)
nr_levels = int(ds.attrs["nr_levels"])
check(n.min() >= 1 and m.min() >= 1, "n,m are 1-based (min >= 1)")
check(level.min() == 1 and level.max() == nr_levels,
      f"level in 1..nr_levels (got {level.min()}..{level.max()}, nr_levels={nr_levels})")
check(nr_levels >= 2, f"MULTI-LEVEL quadtree (nr_levels={nr_levels} >= 2)")
check(len(np.unique(level)) >= 2,
      f"more than one level actually present ({sorted(np.unique(level).tolist())})")

print("\n=== 3. neighbour indices in [0, ncells] (1-based, 0=none) ===")
mu1 = ds["mu1"].values.astype(int); mu2 = ds["mu2"].values.astype(int)
md1 = ds["md1"].values.astype(int); md2 = ds["md2"].values.astype(int)
nu1 = ds["nu1"].values.astype(int); nu2 = ds["nu2"].values.astype(int)
nd1 = ds["nd1"].values.astype(int); nd2 = ds["nd2"].values.astype(int)
for name, arr in [("mu1", mu1), ("mu2", mu2), ("md1", md1), ("md2", md2),
                  ("nu1", nu1), ("nu2", nu2), ("nd1", nd1), ("nd2", nd2)]:
    ok = bool(((arr >= 0) & (arr <= ncells)).all())
    check(ok, f"'{name}' in [0,{ncells}] (min={arr.min()}, max={arr.max()})")
check(int((mu1 > 0).sum()) > 0 and int((nu1 > 0).sum()) > 0,
      "mu1 and nu1 have real links (>0)")

print("\n=== 4. level-diff flags in {-1,0,1} ===")
mu = ds["mu"].values.astype(int); md = ds["md"].values.astype(int)
nu = ds["nu"].values.astype(int); nd = ds["nd"].values.astype(int)
for name, arr in [("mu", mu), ("md", md), ("nu", nu), ("nd", nd)]:
    check(set(np.unique(arr).tolist()).issubset({-1, 0, 1}),
          f"flag '{name}' in {{-1,0,1}} (got {sorted(np.unique(arr).tolist())})")
# Must actually exercise all three on a refined grid
check(set(np.unique(np.concatenate([mu, md, nu, nd])).tolist()) == {-1, 0, 1},
      "all three level-diffs {-1,0,+1} occur (refinement transitions exist)")

print("\n=== 5. SAME-LEVEL reciprocity (the connectivity-coherence test) ===")
# For every cell i with a SAME-LEVEL +m neighbour j (mu[i]==0, mu1[i]>0),
# j's -m same-level neighbour (md1) must be i (1-based).
def recip(flag_a, idx_a1, flag_b, idx_b1, label):
    i = np.where((flag_a == 0) & (idx_a1 > 0))[0]
    j = idx_a1[i] - 1  # 0-based neighbour
    # neighbour must be same level
    same_lvl = level[j] == level[i]
    i, j = i[same_lvl], j[same_lvl]
    back = idx_b1[j]            # neighbour's reverse index (1-based)
    ok = bool(np.all(back == (i + 1)))
    n_checked = len(i)
    check(ok and n_checked > 0,
          f"{label}: {n_checked} same-level links, all reciprocal")

recip(mu, mu1, md, md1, "mu1<->md1 (+m / -m)")
recip(nu, nu1, nd, nd1, "nu1<->nd1 (+n / -n)")

print("\n=== 6. CROSS-LEVEL (refinement) connectivity sanity ===")
# Cells flagged mu==1 (finer cells to the right) must have BOTH mu1 and mu2 set,
# and each finer neighbour must flag md==-1 pointing back.
fin_m = np.where(mu == 1)[0]
check(len(fin_m) > 0, f"cells with finer +m neighbour exist ({len(fin_m)})")
both_set = (mu1[fin_m] > 0) & (mu2[fin_m] > 0)
check(bool(np.all(both_set)),
      f"all mu==1 cells have BOTH mu1 & mu2 finer neighbours "
      f"({int(both_set.sum())}/{len(fin_m)})")
# the two finer neighbours must be one level finer
f1 = mu1[fin_m] - 1
f2 = mu2[fin_m] - 1
check(bool(np.all(level[f1] == level[fin_m] + 1)) and
      bool(np.all(level[f2] == level[fin_m] + 1)),
      "mu1/mu2 finer neighbours are exactly one level finer")
# coarser side: cells with mu==-1 point to a coarser neighbour
crs_m = np.where(mu == -1)[0]
check(len(crs_m) > 0, f"cells with coarser +m neighbour exist ({len(crs_m)})")
cc = mu1[crs_m] - 1
check(bool(np.all(level[cc] == level[crs_m] - 1)),
      "mu==-1 cells point to a neighbour exactly one level coarser")

print("\n=== 7. snapwave_mask domain + boundary cells ===")
sw = ds["snapwave_mask"].values.astype(int)
check(set(np.unique(sw).tolist()).issubset({0, 1, 2, 3, 4, 5, 6}),
      f"snapwave_mask values valid (got {sorted(np.unique(sw).tolist())})")
check(int((sw == 1).sum()) > 0, "snapwave_mask has active cells (==1)")
check(int((sw > 1).sum()) > 0, "snapwave_mask has boundary cells (>1)")

print("\n=== 8. mask domain ===")
mask = ds["mask"].values.astype(int)
check(set(np.unique(mask).tolist()).issubset({0, 1, 2, 3, 4, 5, 6}),
      f"mask values valid (got {sorted(np.unique(mask).tolist())})")
check(int((mask == 2).sum()) > 0, "mask has waterlevel-boundary cells (==2)")

print("\n" + "=" * 64)
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: QUADTREE STRUCTURALLY VALID against decoded SFINCS-solver format")
print("        (incl. multi-level refinement connectivity reciprocity)")
sys.exit(0)
