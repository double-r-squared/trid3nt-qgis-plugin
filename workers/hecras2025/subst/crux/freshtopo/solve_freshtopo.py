#!/usr/bin/env python3
"""Solve the fresh-topology pure-2D deck through the PRODUCTION 6.x engines.

Runs inside ``trid3nt-local/hecras:latest`` with the venv python. The rundir
(built by ``build_freshtopo_deck.py``) is bind-mounted; this runs
``RasGeomPreprocess`` then ``RasUnsteady`` and reports the 2D solve metrics.

  docker run --rm -v <rundir>:/run -v <freshtopo>:/ft:ro --entrypoint bash \
    trid3nt-local/hecras:latest -lc \
    '/opt/trid3nt/.venv/bin/python /ft/solve_freshtopo.py /run'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np

BIN = Path(os.environ.get("TRID3NT_HECRAS_BIN_DIR", "/opt/hecras/bin"))
LIBS = Path("/opt/hecras/libs")
PLAN = "Fresh2D.p04.tmp.hdf"
GEOM = "x04"
AREA = "/Geometry/2D Flow Areas/2D Interior Area"
_FILL = 1e30


def _env():
    e = dict(os.environ)
    e["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(LIBS), str(LIBS / "mkl"), str(LIBS / "rhel_8"), e.get("LD_LIBRARY_PATH", "")])
    e["PATH"] = os.pathsep.join([str(BIN), e.get("PATH", "")])
    return e


def _run(engine, cwd):
    p = subprocess.run([str(BIN / engine), PLAN, GEOM], cwd=str(cwd), env=_env(),
                       capture_output=True, text=True, timeout=3600)
    fin = "Finished" in p.stdout
    print(f"  $ {engine} -> exit={p.returncode} finished={fin}")
    if p.returncode != 0 or not fin:
        print("---- STDOUT tail ----")
        print(p.stdout[-3000:])
        print("---- STDERR tail ----", file=sys.stderr)
        print(p.stderr[-1500:], file=sys.stderr)
        raise SystemExit(f"FAIL: {engine} did not finish cleanly")
    print("    " + [l for l in p.stdout.splitlines() if "Finished" in l][-1].strip())


def metrics(plan_path: Path) -> dict:
    with h5py.File(plan_path, "r") as f:
        if "Results" not in f:
            raise SystemExit("FAIL: no /Results group after RasUnsteady")
        va = f["Results/Unsteady/Summary/Volume Accounting"]
        err = abs(float(va.attrs["Error Percent"]))
        base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"
        mw = f[f"{base}/2D Flow Areas/2D Interior Area/Maximum Water Surface"][()]
        a = np.where(np.abs(mw) > _FILL, np.nan, mw).astype(np.float64)
        if a.ndim == 2:
            a = a[0] if a.shape[0] < a.shape[1] else a[:, 0]
        minel = f[f"{AREA}/Cells Minimum Elevation"][()].astype(np.float64)
        minel = np.where(np.abs(minel) > _FILL, np.nan, minel)
        n = min(a.shape[-1], minel.shape[0])
        wse = a[:n]
        mn = minel[:n]
        depth = wse - mn
        wet = int(np.nansum(depth > 0.01))
        return {
            "wse_max_ft": float(np.nanmax(wse)),
            "wse_min_ft": float(np.nanmin(wse)),
            "depth_max_ft": float(np.nanmax(depth)),
            "wet_cells": wet,
            "vol_err_pct": err,
            "flux_in": float(va.attrs["Total Boundary Flux of Water In"]),
            "flux_out": float(va.attrs["Total Boundary Flux of Water Out"]),
        }


def main():
    rd = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(f"=== FRESH-TOPOLOGY SOLVE  rundir={rd} ===")
    print("[1] RasGeomPreprocess (fake-reach 1D; preserves the carved 2D tables)")
    _run("RasGeomPreprocess", rd)
    print("[2] RasUnsteady (2D subgrid solve on the FRESH tessellation)")
    _run("RasUnsteady", rd)
    m = metrics(rd / PLAN)
    print("\n=== RESULT ===")
    print(f"  maxWSE={m['wse_max_ft']:.3f} ft  minWSE={m['wse_min_ft']:.3f}  "
          f"maxDepth={m['depth_max_ft']:.2f} ft  wet_cells={m['wet_cells']}")
    print(f"  vol_err={m['vol_err_pct']:.6f}%  flux in/out={m['flux_in']:.1f}/{m['flux_out']:.1f}")
    (rd / "freshtopo_result.json").write_text(json.dumps(m, indent=2))
    print(f"  wrote {rd/'freshtopo_result.json'}")


if __name__ == "__main__":
    main()
