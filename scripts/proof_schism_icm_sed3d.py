"""Discriminating-pair proof figures for (SCHISM ICM + SED3D targeted
binaries). Filled tricontourf fields (never scatter) on the georeferenced estuary
mesh with the mesh wireframe overlaid:

  * SED3D: surface suspended concentration, FINE class vs COARSE class -- fine
    (low settling velocity) stays suspended and travels downstream; coarse settles
    near the river source. Identical forcing; the settling velocity is the only
    difference.
  * ICM: column-max NH4 (ammonium), nutrient LOAD vs NO-LOAD river input -- a
    downstream nutrient plume appears only under load.

Idealized channel geometry (Galveston Bay footprint; the honesty floor -- not a
surveyed estuary), so the figures are labelled IDEALIZED.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.tri as mtri  # noqa: E402
import numpy as np  # noqa: E402
from netCDF4 import Dataset  # noqa: E402

SCR = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
           "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad")
OUT = Path("/home/nate/Documents/trid3nt-local/docs/proof/templates")


def _mesh(rundir: Path):
    lines = (rundir / "hgrid.gr3").read_text().splitlines()
    ne, nn = (int(v) for v in lines[1].split()[:2])
    xy = np.array([[float(lines[2 + i].split()[1]), float(lines[2 + i].split()[2])]
                   for i in range(nn)])
    tris = np.array([[int(x) - 1 for x in lines[2 + nn + e].split()[2:5]]
                     for e in range(ne)])
    return xy, tris


def _surf(rundir: Path, ncfile: str, var: str):
    ds = Dataset(rundir / "outputs" / ncfile)
    a = np.asarray(ds.variables[var][:])  # (time, node, layer)
    ds.close()
    col = a[-1, :, :]                      # last time step, all vertical layers
    col = np.where(np.isfinite(col), col, 0.0)
    return col.max(axis=1)                 # peak concentration in the water column


def panel(ax, xy, tris, vals, title, cmap, vmin, vmax, unit):
    tri = mtri.Triangulation(xy[:, 0], xy[:, 1], tris)
    lv = np.linspace(vmin, vmax, 13)
    tcf = ax.tricontourf(tri, vals, levels=lv, cmap=cmap, extend="max")
    ax.triplot(tri, color="k", lw=0.15, alpha=0.35)  # mesh wireframe
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("lon"); ax.set_ylabel("lat")
    ax.set_aspect("equal")
    cb = plt.colorbar(tcf, ax=ax, shrink=0.8)
    cb.set_label(unit, fontsize=8)


def sed_figure():
    rd = SCR / "sed_smoke" / "run_multiclass"
    xy, tris = _mesh(rd)
    fine = _surf(rd, "sedConcentration_1_1.nc", "sedConcentration_1")
    coarse = _surf(rd, "sedConcentration_2_1.nc", "sedConcentration_2")
    vmax = float(max(fine.max(), coarse.max()))
    fig, axs = plt.subplots(1, 2, figsize=(11, 5))
    panel(axs[0], xy, tris, fine,
          f"FINE class (Wsed 1.06 mm/s)\ncolumn-max SSC  max {fine.max():.4f} kg/m3",
          "YlOrBr", 0, vmax, "kg/m3")
    panel(axs[1], xy, tris, coarse,
          f"COARSE class (Wsed 28.65 mm/s)\ncolumn-max SSC  max {coarse.max():.4f} kg/m3",
          "YlOrBr", 0, vmax, "kg/m3")
    fig.suptitle("SCHISM SED3D multiclass suspended transport (pschism_SED_TVD-VL) "
                 "-- IDEALIZED Galveston Bay channel\nfine stays suspended / coarse "
                 "settles near the river source; identical tidal+river forcing "
                 "", fontsize=10)
    fig.tight_layout()
    p = OUT / "schism_sed3d_multiclass_settling.png"
    fig.savefig(p, dpi=130); plt.close(fig)
    return p, float(fine.max()), float(coarse.max())


def icm_figure():
    load = SCR / "icm_smoke" / "run_load"
    noload = SCR / "icm_smoke" / "run_noload"
    xy, tris = _mesh(load)
    nh4_l = _surf(load, "ICM_NH4_1.nc", "ICM_NH4")
    nh4_n = _surf(noload, "ICM_NH4_1.nc", "ICM_NH4")
    vmax = float(max(nh4_l.max(), nh4_n.max()))
    fig, axs = plt.subplots(1, 2, figsize=(11, 5))
    panel(axs[0], xy, tris, nh4_n,
          f"NO-LOAD (background river)\ncolumn-max NH4  max {nh4_n.max():.3f} g/m3",
          "GnBu", 0, vmax, "g/m3 (NH4-N)")
    panel(axs[1], xy, tris, nh4_l,
          f"NUTRIENT LOAD (river NH4 3.0 g/m3)\ncolumn-max NH4  max {nh4_l.max():.3f} g/m3",
          "GnBu", 0, vmax, "g/m3 (NH4-N)")
    fig.suptitle("SCHISM ICM eutrophication core (pschism_ICM_TVD-VL) -- IDEALIZED "
                 "Galveston Bay channel\nnutrient plume appears only under river "
                 "loading; 17-var water-quality kinetics", fontsize=10)
    fig.tight_layout()
    p = OUT / "schism_icm_eutrophication_load.png"
    fig.savefig(p, dpi=130); plt.close(fig)
    return p, float(nh4_l.max()), float(nh4_n.max())


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    sp, fmax, cmax = sed_figure()
    ip, lmax, nmax = icm_figure()
    print(f"SED  proof: {sp}  fine_max={fmax:.4f} coarse_max={cmax:.4f} "
          f"ratio={fmax / max(cmax, 1e-12):.2f}")
    print(f"ICM  proof: {ip}  load_NH4_max={lmax:.3f} noload_NH4_max={nmax:.3f} "
          f"ratio={lmax / max(nmax, 1e-12):.2f}")
