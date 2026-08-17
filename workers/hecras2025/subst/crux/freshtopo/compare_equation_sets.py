#!/usr/bin/env python3
"""Diffusion-Wave vs full-SWE regression on ONE authored fresh-AOI 2D deck.

Reference driver for the ``2d_diffusion_wave_vs_full_swe_regression`` board row: it
authors a single steep-AOI mesh ONCE, stamps the two 2D equation sets on two copies
of the identical plan HDF, solves both through the production 6.6 RasUnsteady, and
reports how the peak-inundation deliverable and the per-cell water surface differ.

Finding (Blanco River canyon nr Wimberley TX, 329 ft relief, 15000 cfs):
the two solvers agree on the peak-inundation ENVELOPE (wet extent, max depth, max
WSE identical to sub-inch) and separate ONLY at a small set (~0.3% of cells, up to
~1.9 ft) of momentum-dominated cells -- the localized inertial signature. Diffusion
Wave is the cheaper default; full SWE matters where local inertia does. This
extends the low-gradient Muncie coincidence into a steep, dry, dynamics
driven regime.

The comparison is a composed analysis (two engine runs + a host-side diff), not a
new agent tool: the agent runs it by calling ``hecras_flood_2d`` twice with
``equation_set`` = diffusion_wave / full_swe and differencing the two depth COGs in
the playground. This driver is the durable, reproducible reference for that.

Run (repo root, env loaded):
  set -a; source .env.local; set +a
  venvs/agent/bin/python workers/hecras2025/subst/crux/freshtopo/\
compare_equation_sets.py <dem.tif> <workdir> [--peak-cfs F] [--resolution-m M]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np

_HERE = Path(__file__).resolve().parent
_HECRAS2025 = _HERE.parents[2]
for _p in (str(_HERE), str(_HECRAS2025)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from flood2d_pipeline import author_and_compose, SOLVER_IMAGE_DEFAULT  # noqa: E402

AREA = "2D Interior Area"
_FILL = 1e30
_BASE = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"


def _solve(deck_dir: Path, image: str = SOLVER_IMAGE_DEFAULT) -> None:
    argv = [
        "docker", "run", "--rm", "-v", f"{deck_dir}:/run", "-v", f"{_HERE}:/ft:ro",
        "--entrypoint", "bash", image,
        "-lc", "/opt/trid3nt/.venv/bin/python /ft/solve_freshtopo.py /run",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or "Finished Unsteady" not in proc.stdout:
        raise SystemExit(f"solve failed ({deck_dir.name}):\n{proc.stdout[-2000:]}\n{proc.stderr[-800:]}")


def _cells(plan: Path):
    """Per-cell max WSE + min elevation + center coords from a solved plan HDF."""
    with h5py.File(plan, "r") as f:
        mw = np.asarray(f[f"{_BASE}/2D Flow Areas/{AREA}/Maximum Water Surface"][()], float)
        mw = np.where(np.abs(mw) > _FILL, np.nan, mw)
        if mw.ndim == 2:
            mw = mw[0] if mw.shape[0] < mw.shape[1] else mw[:, 0]
        g = f[f"Geometry/2D Flow Areas/{AREA}"]
        me = np.where(np.abs(np.asarray(g["Cells Minimum Elevation"][()], float)) > _FILL,
                      np.nan, np.asarray(g["Cells Minimum Elevation"][()], float))
        cc = np.asarray(g["Cells Center Coordinate"][()], float)
    return mw, me, cc


def compare(dem_tif: str, workdir: str, *, peak_cfs: float = 15000.0,
            resolution_m: float = 30.0, wet_thresh_ft: float = 0.1) -> dict:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    result, info = author_and_compose(dem_tif, str(workdir / "author"), peak_cfs=peak_cfs,
                                      resolution_m=resolution_m, equation_set="Diffusion Wave")
    dw = workdir / "deck_dw"
    swe = workdir / "deck_swe"
    for d in (dw, swe):
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(result.deck_dir, d)
    with h5py.File(swe / "Fresh2D.p04.tmp.hdf", "r+") as f:
        f["Plan Data/Plan Parameters"].attrs["2D Equation Set"] = np.bytes_(b"SWE-ELM")
    _solve(dw)
    _solve(swe)
    mwd, me, cc = _cells(dw / "Fresh2D.p04.tmp.hdf")
    mws, _, _ = _cells(swe / "Fresh2D.p04.tmp.hdf")
    n = min(len(mwd), len(mws), len(me), len(cc))
    mwd, mws, me, cc = mwd[:n], mws[:n], me[:n], cc[:n]
    dd = np.clip(mwd - me, 0, None)
    ds = np.clip(mws - me, 0, None)
    val = np.isfinite(mwd) & np.isfinite(mws) & np.isfinite(me)
    dv = np.abs((mwd - mws)[val])
    out = {
        "cells": int(n), "peak_cfs": peak_cfs, "resolution_m": resolution_m,
        "terrain_relief_ft": round(result.terrain_max_ft - result.terrain_min_ft, 1),
        "wet_cells_dw": int((dd > wet_thresh_ft).sum()),
        "wet_cells_swe": int((ds > wet_thresh_ft).sum()),
        "max_depth_dw_ft": round(float(np.nanmax(dd)), 3),
        "max_depth_swe_ft": round(float(np.nanmax(ds)), 3),
        "dwse_absmax_ft": round(float(dv.max()), 4),
        "dwse_p99_ft": round(float(np.percentile(dv, 99)), 4),
        "cells_gt_0p1ft": int((dv > 0.1).sum()),
        "cells_gt_0p5ft": int((dv > 0.5).sum()),
        "cells_gt_1p0ft": int((dv > 1.0).sum()),
        "pct_cells_gt_0p1ft": round(100 * float(np.mean(dv > 0.1)), 3),
    }
    np.savez(workdir / "eqset_diff.npz", cc=cc, me=me, mwd=mwd, mws=mws)
    (workdir / "eqset_compare.json").write_text(json.dumps(out, indent=2))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dem_tif")
    ap.add_argument("workdir")
    ap.add_argument("--peak-cfs", type=float, default=15000.0)
    ap.add_argument("--resolution-m", type=float, default=30.0)
    a = ap.parse_args()
    out = compare(a.dem_tif, a.workdir, peak_cfs=a.peak_cfs, resolution_m=a.resolution_m)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
