#!/usr/bin/env python3
"""2D-model stability diagnostic sweep on ONE authored fresh-AOI deck.

Reference driver for the ``2d_model_stability_diagnostic_sweep`` board row: it
authors a single mesh ONCE, then re-solves the identical deck at a descending
ladder of computation intervals (time steps) and reports how the peak water
surface and volume error converge as the step tightens -- the automated analogue
of the published Bald Eagle Creek 5-trial convergence path.

Finding (Blanco River canyon nr Wimberley TX, 8075 cells, 15000 cfs):
the coarse 10MIN step is numerically UNSTABLE (max depth overshoots to 487 ft),
and tightening the step collapses the spurious spike monotonically --
  10MIN -> 487.5 ft ; 5MIN -> 244.8 ft ; 2MIN -> 116.1 ft ; 1MIN -> 116.1 ft --
so 2MIN is the converged step (the 2MIN->1MIN peak change is 0.04 ft, matching the
published Bald Eagle ~0.05 ft max-WSE convergence anchor). Volume error stays
sub-0.02% throughout (a secondary signal here); the primary stability diagnostic is
the peak-WSE stabilization.

COMPOSABLE stability knobs on our fresh-AOI deck surface today: the computation
interval (this sweep) and the downstream normal-depth slope (``ds_slope`` on the
composer). NOT composable on an authored pure-2D deck (recipe, not built): culvert-
invert raising (the composer strips all Structures) and cell re-alignment (the mesh
is fixed by the AuthorMesh topology). This driver lands the composable subset.

Run (repo root, env loaded):
  set -a; source .env.local; set +a
  venvs/agent/bin/python workers/hecras2025/subst/crux/freshtopo/\
stability_sweep.py <dem.tif> <workdir> [--intervals 10MIN 5MIN 2MIN 1MIN]
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
from hecras_deck2d import _patch_computation_interval  # noqa: E402

DEFAULT_INTERVALS = ["10MIN", "5MIN", "2MIN", "1MIN"]


def _solve(deck_dir: Path, image: str = SOLVER_IMAGE_DEFAULT) -> dict:
    argv = [
        "docker", "run", "--rm", "-v", f"{deck_dir}:/run", "-v", f"{_HERE}:/ft:ro",
        "--entrypoint", "bash", image,
        "-lc", "/opt/trid3nt/.venv/bin/python /ft/solve_freshtopo.py /run",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    res = deck_dir / "freshtopo_result.json"
    if proc.returncode != 0 or not res.exists():
        raise SystemExit(f"solve failed ({deck_dir.name}):\n{proc.stdout[-2000:]}\n{proc.stderr[-800:]}")
    return json.loads(res.read_text())


def sweep(dem_tif: str, workdir: str, *, peak_cfs: float = 15000.0,
          resolution_m: float = 30.0, intervals: list[str] | None = None) -> dict:
    intervals = intervals or DEFAULT_INTERVALS
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    result, _ = author_and_compose(dem_tif, str(workdir / "author"), peak_cfs=peak_cfs,
                                   resolution_m=resolution_m, equation_set="Diffusion Wave")
    fresh = Path(result.deck_dir)
    trials = []
    for iv in intervals:
        d = workdir / f"trial_{iv}"
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(fresh, d)
        b04 = d / "Fresh2D.b04"
        b04.write_text(_patch_computation_interval(b04.read_text(), iv))
        m = _solve(d)
        trials.append({
            "interval": iv,
            "max_depth_ft": round(m["depth_max_ft"], 3),
            "max_wse_ft": round(m["wse_max_ft"], 3),
            "wet_cells": m["wet_cells"],
            "vol_err_pct": m["vol_err_pct"],
        })
    depths = [t["max_wse_ft"] for t in trials]
    converged_step = intervals[-1]
    for i in range(1, len(trials)):
        if abs(trials[i]["max_wse_ft"] - trials[i - 1]["max_wse_ft"]) <= 0.05:
            converged_step = trials[i - 1]["interval"]
            break
    out = {
        "cells": int(result.cells_real), "peak_cfs": peak_cfs, "resolution_m": resolution_m,
        "terrain_relief_ft": round(result.terrain_max_ft - result.terrain_min_ft, 1),
        "trials": trials,
        "max_wse_spread_ft": round(max(depths) - min(depths), 3),
        "converged_step": converged_step,
        "final_peak_change_ft": round(abs(trials[-1]["max_wse_ft"] - trials[-2]["max_wse_ft"]), 3)
        if len(trials) >= 2 else None,
    }
    (workdir / "stability_sweep.json").write_text(json.dumps(out, indent=2))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dem_tif")
    ap.add_argument("workdir")
    ap.add_argument("--peak-cfs", type=float, default=15000.0)
    ap.add_argument("--resolution-m", type=float, default=30.0)
    ap.add_argument("--intervals", nargs="+", default=None)
    a = ap.parse_args()
    out = sweep(a.dem_tif, a.workdir, peak_cfs=a.peak_cfs, resolution_m=a.resolution_m,
                intervals=a.intervals)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
