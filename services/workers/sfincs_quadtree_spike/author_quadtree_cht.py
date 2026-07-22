#!/usr/bin/env python3
"""SCRATCH SPIKE — author a MULTI-LEVEL refined SFINCS quadtree + SnapWave deck
with Deltares cht_sfincs (Coastal Hazards Toolkit).

This is the GATE the coastal North Star hinges on: hydromt-sfincs 1.2.2 can
READ/WRITE a quadtree deck but cannot BUILD a refined one from scratch — the
multi-level (x2 refine, nr_levels up to 6) neighbour-index connectivity table
(mu1/nu1/md1/nd1 + level flags) is the load-bearing unknown. cht_sfincs's
QuadtreeGrid.get_neighbors() computes exactly that table for the refined case.

We author a tiny SYNTHETIC coastal AOI:
  * sloping beach bathy: deep offshore (low m, west) -> high+dry onshore (east)
  * ONE refinement polygon near the coast => nr_levels = 3 (x2 twice)
  * SFINCS mask: offshore column = waterlevel boundary (2), wet/surf = active (1)
  * SnapWave mask: offshore = wave boundary (2), wet/surf = active (1)

Then SFINCS.write() emits sfincs.nc (quadtree netcdf) + sfincs.inp + the
SnapWave boundary files. validate_quadtree_cht.py checks the produced netcdf
against the decoded SFINCS-solver Fortran format.
"""
from __future__ import annotations

import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import xarray as xr
import xugrid as xu
from pyproj import CRS
from shapely.geometry import Polygon

from cht_sfincs import SFINCS

HERE = Path(__file__).resolve().parent
DECK = HERE / "deck_cht"
DECK.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Grid geometry — tiny synthetic coastal AOI, UTM 16N (Mexico Beach zone).
# --------------------------------------------------------------------------- #
EPSG = 32616
crs = CRS.from_epsg(EPSG)
x0, y0 = 600000.0, 3200000.0
dx = dy = 200.0          # 200 m coarse cell; refines to 50 m at level 3
nmax = 16                # rows (n, y)
mmax = 24                # cols (m, x); m increases eastward (onshore)
rotation = 0.0

# Coastal refinement polygon: a band over the surf zone / nearshore (mid-east
# part of the domain) refined twice (refinement_level=2 -> levels 1,2,3).
xa = x0 + 8 * dx
xb = x0 + 18 * dx
ya = y0 + 2 * dy
yb = y0 + 14 * dy
ref_poly = Polygon([(xa, ya), (xb, ya), (xb, yb), (xa, yb)])
refinement_polygons = gpd.GeoDataFrame(
    {"refinement_level": [2], "geometry": [ref_poly]}, crs=crs
)

print("=== 1. Build refined quadtree via cht_sfincs ===")
sf = SFINCS(root=str(DECK), crs=EPSG, mode="w")
sf.grid.build(
    x0, y0, nmax, mmax, dx, dy, rotation,
    refinement_polygons=refinement_polygons,
)
nr_cells = sf.grid.data.sizes["mesh2d_nFaces"]
nr_levels = int(sf.grid.data.attrs["nr_levels"])
print(f"[ok] built quadtree: nr_cells={nr_cells} nr_levels={nr_levels}")
print(f"     level histogram: "
      f"{np.bincount(sf.grid.data['level'].values.astype(int))[1:]}")

# --------------------------------------------------------------------------- #
# 2. Bathymetry: sloping beach profile from cell-centre x.
#    z = -8 m at x0 (west/offshore) -> +4 m at east edge (onshore dune).
# --------------------------------------------------------------------------- #
print("\n=== 2. Set synthetic sloping bathymetry ===")
xc, yc = sf.grid.face_coordinates()
x_span = mmax * dx
zb = -8.0 + 12.0 * (xc - x0) / x_span
ugrid2d = sf.grid.data.grid
sf.grid.data["z"] = xu.UgridDataArray(
    xr.DataArray(data=zb.astype("float32"), dims=[ugrid2d.face_dimension]),
    ugrid2d,
)
print(f"[ok] z range: {zb.min():.2f} .. {zb.max():.2f} m "
      f"(offshore deep -> onshore high)")

# --------------------------------------------------------------------------- #
# 3. SFINCS active mask: build via cht polygon API.
#    - active (1) where z below +2 m (wet + surf zone)
#    - waterlevel boundary (2) at the offshore (west) edge column
# --------------------------------------------------------------------------- #
print("\n=== 3. Build SFINCS mask (active + waterlevel boundary) ===")
# include everything below +2m as active; open (waterlevel) boundary in a thin
# offshore strip at the west edge.
offshore_strip = gpd.GeoDataFrame(
    {"geometry": [Polygon([
        (x0 - dx, y0 - dy),
        (x0 + 1.0 * dx, y0 - dy),
        (x0 + 1.0 * dx, y0 + (nmax + 1) * dy),
        (x0 - dx, y0 + (nmax + 1) * dy),
    ])]},
    crs=crs,
)
sf.mask.build(
    zmin=-100.0, zmax=2.0,                 # active cells: z in [-100, +2]
    open_boundary_polygon=offshore_strip,  # waterlevel boundary (mask=2)
    open_boundary_zmin=-100.0, open_boundary_zmax=2.0,
)
mvals = sf.grid.data["mask"].values
print(f"[ok] mask histogram: "
      f"active(1)={int((mvals==1).sum())} "
      f"wlbnd(2)={int((mvals==2).sum())} "
      f"inactive(0)={int((mvals==0).sum())}")

# --------------------------------------------------------------------------- #
# 4. SnapWave mask: active (1) over wet/surf, wave boundary (2) offshore.
# --------------------------------------------------------------------------- #
print("\n=== 4. Build SnapWave mask (active + wave boundary) ===")
sf.snapwave.mask.build(
    zmin=-100.0, zmax=2.0,
    open_boundary_polygon=offshore_strip,   # wave-energy boundary
    open_boundary_zmin=-100.0, open_boundary_zmax=2.0,
)
swvals = sf.grid.data["snapwave_mask"].values
print(f"[ok] snapwave_mask histogram: "
      f"active(1)={int((swvals==1).sum())} "
      f"wavebnd={int((swvals>1).sum())} "
      f"inactive(0)={int((swvals==0).sum())}")

# --------------------------------------------------------------------------- #
# 5. SnapWave boundary forcing (incident wave time series at offshore point).
# --------------------------------------------------------------------------- #
print("\n=== 5. SnapWave boundary forcing (Hm0/Tp/dir/spread) ===")
import datetime
t0 = datetime.datetime(2018, 10, 10, 0, 0, 0)
t1 = datetime.datetime(2018, 10, 10, 2, 0, 0)
bx = x0 + 0.5 * dx
by = y0 + (nmax / 2) * dy
try:
    sf.snapwave.boundary_conditions.add_point(
        bx, by, hs=3.0, tp=12.0, wd=270.0, ds=20.0
    )
    sf.snapwave.boundary_conditions.set_timeseries_uniform(
        hs=3.0, tp=12.0, wd=270.0, ds=20.0, t0=t0, t1=t1
    )
    print("[ok] snapwave bnd point + uniform timeseries set via cht API")
    snapwave_forced = True
except Exception as exc:  # noqa: BLE001
    print(f"[info] cht snapwave boundary API path raised: "
          f"{type(exc).__name__}: {exc}")
    print("       (will hand-write snapwave.bnd/bhs/btp/bwd/bds below)")
    snapwave_forced = False

# --------------------------------------------------------------------------- #
# 6. sfincs.inp keywords (snapwave coupling + grid + time + output).
# --------------------------------------------------------------------------- #
print("\n=== 6. Set sfincs.inp keywords ===")
v = sf.input.variables
v.qtrfile = "sfincs.nc"
v.x0, v.y0, v.dx, v.dy = x0, y0, dx, dy
v.nmax, v.mmax, v.rotation = nmax, mmax, rotation
v.epsg = EPSG
v.tref, v.tstart, v.tstop = t0, t0, t1
v.dtout = 600.0
v.dtmaxout = 600.0
# SnapWave coupling
v.snapwave = True
v.snapwave_bndfile = "snapwave.bnd"
v.snapwave_bhsfile = "snapwave.bhs"
v.snapwave_btpfile = "snapwave.btp"
v.snapwave_bwdfile = "snapwave.bwd"
v.snapwave_bdsfile = "snapwave.bds"
print("[ok] sfincs.inp variables set (snapwave=True, qtrfile=sfincs.nc)")

# --------------------------------------------------------------------------- #
# 7. Write the whole deck.
# --------------------------------------------------------------------------- #
print("\n=== 7. Write deck (sfincs.nc + sfincs.inp + forcing) ===")
sf.write()

# If the cht snapwave forcing API didn't take, hand-write the boundary files so
# the deck is complete (these are the documented snapwave_* ascii files).
def _hand_write_snapwave_forcing():
    (DECK / "snapwave.bnd").write_text(f"{bx:.2f} {by:.2f}\n")
    (DECK / "snapwave.bhs").write_text("0.0 3.0\n7200.0 3.0\n")
    (DECK / "snapwave.btp").write_text("0.0 12.0\n7200.0 12.0\n")
    (DECK / "snapwave.bwd").write_text("0.0 270.0\n7200.0 270.0\n")
    (DECK / "snapwave.bds").write_text("0.0 20.0\n7200.0 20.0\n")

for fn in ["snapwave.bnd", "snapwave.bhs", "snapwave.btp",
           "snapwave.bwd", "snapwave.bds"]:
    if not (DECK / fn).exists():
        _hand_write_snapwave_forcing()
        print("[info] hand-wrote snapwave forcing files (cht API path skipped)")
        break

print("\n=== DECK CONTENTS ===")
for p in sorted(DECK.iterdir()):
    if p.is_file():
        print(f"  {p.name:28s} {p.stat().st_size:>10d} bytes")
print(f"\n[done] deck at {DECK}")
