#!/usr/bin/env python3
"""SnapWave + quadtree DECK-AUTHORING SPIKE (scratch — Agent B gate).

Hand-authors a MINIMAL SFINCS quadtree + SnapWave deck for a tiny synthetic
coastal AOI, with NO dependency on a `setup_snapwave` (hydromt-sfincs 1.2.2 has
none) and NO quadtree-from-scratch builder (hydromt-sfincs 1.2.2 only READS
quadtree netcdf, never authors one). Everything below is authored by hand from
the SFINCS Fortran source contract:

  * quadtree netcdf variable + attr schema  -> source/src/sfincs_quadtree.F90
  * snapwave_* sfincs.inp keywords          -> source/src/sfincs_snapwave.f90
  * wave-field output variables             -> source/src/sfincs_ncoutput.F90

The grid is a SINGLE-LEVEL quadtree (nr_levels=1). A single-level quadtree is
structurally identical to a regular grid but stored in the quadtree netcdf
container, so the per-cell neighbour-index connectivity (mu1/nu1/md1/nd1 ...) is
trivially computable from the (n,m) raster indices. This is the smallest object
that exercises the SFINCS quadtree READER + the SnapWave coupling path end to
end. A real Mexico Beach deck would carry nr_levels=6 (Florence config:
refine x2 six times from the offshore ERA5 points toward the coast); the
multi-level neighbour-index computation is the one genuinely hard piece this
spike scopes but does not fully generalise (see report).

Synthetic bathymetry: a beach profile sloping from -8 m offshore (west, low m)
to +4 m onshore (east, high m), so SnapWave has an offshore boundary (msk=2),
a surf zone, and a dry onshore band — the minimal geometry that produces a
non-trivial incident + IG wave transformation.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import xarray as xr
from pyproj import CRS

HERE = Path(__file__).resolve().parent
DECK = HERE / "deck"
DECK.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Grid geometry — tiny synthetic coastal AOI in a projected metric CRS.
# UTM 16N (EPSG:32616) is the Mexico Beach zone; we use a synthetic origin.
# --------------------------------------------------------------------------- #
EPSG = 32616
crs = CRS.from_epsg(EPSG)
x0, y0 = 600000.0, 3200000.0   # arbitrary UTM 16N origin (near Mexico Beach lat)
dx = dy = 50.0                 # 50 m base cell
nmax = 20                      # rows (n, y-direction)
mmax = 30                      # cols (m, x-direction); m increases eastward (onshore)
rotation = 0.0
nr_levels = 1                  # SINGLE-LEVEL quadtree (see module docstring)

# --------------------------------------------------------------------------- #
# Per-cell (n, m) raster indices, 1-based (SFINCS Fortran convention).
# Quadtree cells are flattened column-major-ish; for a single level we simply
# enumerate every (n, m) in the nmax x mmax raster.
# --------------------------------------------------------------------------- #
nn, mm = np.meshgrid(np.arange(1, nmax + 1), np.arange(1, mmax + 1), indexing="ij")
n_flat = nn.ravel().astype(np.int32)   # 1..nmax
m_flat = mm.ravel().astype(np.int32)   # 1..mmax
ncells = n_flat.size                    # nmax*mmax = 600
level = np.ones(ncells, dtype=np.int32)  # all level 1

# Map (n, m) -> flat index (0-based) for neighbour lookups.
def nm_to_idx(n: int, m: int) -> int:
    """1-based (n,m) -> 0-based flat index, or -1 if outside the grid."""
    if n < 1 or n > nmax or m < 1 or m > mmax:
        return -1
    return (m - 1) * nmax + (n - 1)  # column-major: m outer, n inner

# SFINCS neighbour-index arrays. For a single-level grid each cell has exactly
# one neighbour per face (the "1" index); the "2" index (used only where a
# coarse cell abuts two finer cells) is 0 (= no second neighbour). md/mu/nd/nu
# are the "level-difference" flags (0 = same level) which are all 0 here.
# Fortran is 1-based; 0 means "no neighbour".
mu1 = np.zeros(ncells, dtype=np.int32)
mu2 = np.zeros(ncells, dtype=np.int32)
md1 = np.zeros(ncells, dtype=np.int32)
md2 = np.zeros(ncells, dtype=np.int32)
nu1 = np.zeros(ncells, dtype=np.int32)
nu2 = np.zeros(ncells, dtype=np.int32)
nd1 = np.zeros(ncells, dtype=np.int32)
nd2 = np.zeros(ncells, dtype=np.int32)
mu = np.zeros(ncells, dtype=np.int32)   # level diff to the +m neighbour
md = np.zeros(ncells, dtype=np.int32)
nu = np.zeros(ncells, dtype=np.int32)
nd = np.zeros(ncells, dtype=np.int32)

for idx in range(ncells):
    n, m = int(n_flat[idx]), int(m_flat[idx])
    # +m (east / "u" in m-direction), -m (west / "d"), +n (north), -n (south)
    e = nm_to_idx(n, m + 1)
    w = nm_to_idx(n, m - 1)
    no = nm_to_idx(n + 1, m)
    so = nm_to_idx(n - 1, m)
    # Fortran 1-based neighbour indices (0 = none)
    mu1[idx] = (e + 1) if e >= 0 else 0
    md1[idx] = (w + 1) if w >= 0 else 0
    nu1[idx] = (no + 1) if no >= 0 else 0
    nd1[idx] = (so + 1) if so >= 0 else 0

# --------------------------------------------------------------------------- #
# Bathymetry: beach profile. West (low m) deep, east (high m) high+dry.
#   m=1   -> z = -8 m (offshore)
#   m=mmax-> z = +4 m (onshore dune)
# --------------------------------------------------------------------------- #
z = (-8.0 + 12.0 * (m_flat - 1) / (mmax - 1)).astype(np.float32)

# --------------------------------------------------------------------------- #
# SFINCS active mask (msk): 1 = active, 2 = water-level boundary, 3 = outflow.
#   Offshore column (m=1) -> 2 (waterlevel/surge boundary, forced by bzs).
#   Everything else active (1).
# --------------------------------------------------------------------------- #
mask = np.ones(ncells, dtype=np.int32)
mask[m_flat == 1] = 2          # offshore = waterlevel boundary

# --------------------------------------------------------------------------- #
# SnapWave mask (snapwave_mask): 0 inactive, 1 active, 2 wave boundary,
#   3 neumann boundary  (per sfincs_ncoutput.F90 description attr).
#   Offshore column (m=1) -> 2 (incident wave boundary, forced by bhs/btp/bwd).
#   Active wet+surf cells (z < +2 m) -> 1.  Onshore dry band -> 0.
# --------------------------------------------------------------------------- #
snapwave_mask = np.zeros(ncells, dtype=np.int32)
snapwave_mask[z < 2.0] = 1
snapwave_mask[m_flat == 1] = 2

# --------------------------------------------------------------------------- #
# Build the quadtree netcdf. SFINCS reads bare variables n, m, level, md, md1,
# md2, mu, mu1, mu2, nd, nd1, nd2, nu, nu1, nu2, z, mask, snapwave_mask
# (sfincs_quadtree.F90:324-396) + global attrs x0,y0,dx,dy,rotation,nmax,mmax,
# nr_levels + a crs variable carrying crs_wkt (read by hydromt QuadtreeGrid).
# --------------------------------------------------------------------------- #
ds = xr.Dataset(
    data_vars=dict(
        n=("mesh2d_nFaces", n_flat),
        m=("mesh2d_nFaces", m_flat),
        level=("mesh2d_nFaces", level),
        md=("mesh2d_nFaces", md), md1=("mesh2d_nFaces", md1), md2=("mesh2d_nFaces", md2),
        mu=("mesh2d_nFaces", mu), mu1=("mesh2d_nFaces", mu1), mu2=("mesh2d_nFaces", mu2),
        nd=("mesh2d_nFaces", nd), nd1=("mesh2d_nFaces", nd1), nd2=("mesh2d_nFaces", nd2),
        nu=("mesh2d_nFaces", nu), nu1=("mesh2d_nFaces", nu1), nu2=("mesh2d_nFaces", nu2),
        z=("mesh2d_nFaces", z),
        mask=("mesh2d_nFaces", mask),
        snapwave_mask=("mesh2d_nFaces", snapwave_mask),
    ),
    attrs=dict(
        x0=x0, y0=y0, dx=dx, dy=dy, rotation=rotation,
        nmax=nmax, mmax=mmax, nr_levels=nr_levels,
        crs=EPSG,
    ),
)
# crs variable carrying the WKT (hydromt QuadtreeGrid.read expects ds["crs"].crs_wkt)
ds["crs"] = xr.DataArray(np.int32(EPSG))
ds["crs"].attrs["crs_wkt"] = crs.to_wkt()
ds["crs"].attrs["epsg_code"] = f"EPSG:{EPSG}"

qtr_path = DECK / "sfincs.nc"
ds.to_netcdf(qtr_path)
print(f"[ok] wrote quadtree netcdf -> {qtr_path}  ({ncells} cells, nr_levels={nr_levels})")

# --------------------------------------------------------------------------- #
# Boundary forcing files (ascii) for SURGE + WAVES.
# Offshore boundary point at the SW corner of the offshore column.
# --------------------------------------------------------------------------- #
bnd_x = x0 + 0.5 * dx
bnd_y = y0 + (nmax / 2) * dy

# snapwave_bndfile: boundary point coords (x y per line)
(DECK / "snapwave.bnd").write_text(f"{bnd_x:.2f} {bnd_y:.2f}\n")
# snapwave_encfile: enclosure polyline (the offshore edge the wave BC applies to)
(DECK / "snapwave.enc").write_text(
    f"{x0:.2f} {y0:.2f}\n{x0:.2f} {y0 + nmax*dy:.2f}\n"
)
# Wave boundary time series (2 times: t=0 and t=end). Columns: time(s) value.
# snapwave_bhsfile = sig wave height Hm0 (m); btp = peak period Tp (s);
# bwd = mean wave direction (deg nautical); bds = directional spreading (deg).
T_END_S = 7200.0
(DECK / "snapwave.bhs").write_text(f"0.0 3.0\n{T_END_S} 3.0\n")     # Hm0 = 3 m
(DECK / "snapwave.btp").write_text(f"0.0 12.0\n{T_END_S} 12.0\n")   # Tp = 12 s
(DECK / "snapwave.bwd").write_text(f"0.0 270.0\n{T_END_S} 270.0\n") # from west
(DECK / "snapwave.bds").write_text(f"0.0 20.0\n{T_END_S} 20.0\n")   # spread 20 deg

# SFINCS surge boundary: bnd + bzs (slowly varying water level at bnd points)
(DECK / "sfincs.bnd").write_text(f"{bnd_x:.2f} {bnd_y:.2f}\n")
(DECK / "sfincs.bzs").write_text(f"0.0 1.5\n{T_END_S} 2.0\n")       # surge 1.5->2.0 m

print(f"[ok] wrote surge + wave boundary files -> {DECK}")

# --------------------------------------------------------------------------- #
# 2 m-contour observation/run-up paddle line (the wave-paddle line NATE wants).
# In SnapWave the wavemaker/paddle is the wave-boundary; the "2 m-contour
# paddle" for run-up is an OBSERVATION line at the +2 m elevation contour where
# incident waves are converted to long-crested IG run-up. We export it as an
# obs polyline (snapwave obsfile) along the z=+2 m contour (m index where z~2).
# --------------------------------------------------------------------------- #
m_2m = int(np.argmin(np.abs((-8.0 + 12.0 * (np.arange(mmax)) / (mmax - 1)) - 2.0))) + 1
paddle_x = x0 + (m_2m - 0.5) * dx
(DECK / "snapwave_paddle_2m.pol").write_text(
    f"{paddle_x:.2f} {y0:.2f}\n{paddle_x:.2f} {y0 + nmax*dy:.2f}\n"
)
print(f"[ok] wrote 2 m-contour paddle line at m={m_2m} (x={paddle_x:.1f})")

# --------------------------------------------------------------------------- #
# sfincs.inp — main deck. Every snapwave_* keyword is from sfincs_snapwave.f90.
# --------------------------------------------------------------------------- #
inp = f"""\
$ SFINCS + SnapWave quadtree deck (hand-authored spike) — Mexico Beach pattern
$ ---- grid / domain (quadtree) ----
mmax              = {mmax}
nmax              = {nmax}
dx                = {dx}
dy                = {dy}
x0                = {x0}
y0                = {y0}
rotation          = {rotation}
crs               = {EPSG}
qtrfile           = sfincs.nc
$ ---- subgrid (building obstacles + topobathy) ----
sbgfile           = sfincs.sbg
$ ---- time ----
tref              = 20181010 000000
tstart            = 20181010 000000
tstop             = 20181010 020000
dtout             = 600
dtmaxout          = 600
$ ---- physics ----
manning_land      = 0.04
manning_sea       = 0.02
$ ---- SURGE boundary (waterlevel) ----
bndfile           = sfincs.bnd
bzsfile           = sfincs.bzs
$ ---- SnapWave coupling (source/src/sfincs_snapwave.f90) ----
snapwave          = 1
snapwave_bndfile  = snapwave.bnd
snapwave_encfile  = snapwave.enc
snapwave_bhsfile  = snapwave.bhs
snapwave_btpfile  = snapwave.btp
snapwave_bwdfile  = snapwave.bwd
snapwave_bdsfile  = snapwave.bds
snapwave_use_herbers = 1
snapwave_wind     = 0
snapwave_waveforces_ratio = 1.0
$ ---- output ----
outputformat      = net
storehm0          = 1
storehm0ig        = 1
"""
(DECK / "sfincs.inp").write_text(inp)
print(f"[ok] wrote sfincs.inp with snapwave=1 -> {DECK / 'sfincs.inp'}")

print("\n=== DECK CONTENTS ===")
for p in sorted(DECK.iterdir()):
    print(f"  {p.name:28s} {p.stat().st_size:>8d} bytes")
