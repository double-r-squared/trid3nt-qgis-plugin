#!/usr/bin/env python3
"""TRANSPLANT SOLVE -- the M3 strip experiment IN REVERSE (ADR-transplant).

Does the PRODUCTION 6.x RasUnsteady solver consume 2D subgrid property tables
that were authored by an EXTERNAL writer (h5py) rather than by RASMapper? ADR
0100 proved the forward direction (strip the 2D tables -> solver fails,
"Cells Volume Elevation Info doesn't exist"). This proves the reverse: write
2D tables back in via h5py and confirm the 6.x chain (RasGeomPreprocess +
RasUnsteady) reads them and solves.

RasGeomPreprocess does NOT recompute the 2D subgrid tables (-- it only
rebuilds 1D cross-section conveyance), so whatever we write into the plan HDF's
2D `Cells Volume Elevation` / `Faces Area Elevation` groups is exactly what
RasUnsteady solves with. That is the transplant lever.

MODES (env TRANSPLANT_MODE):
  prebuilt  -- the deck at TRANSPLANT_WRK is ALREADY authored (the faithful
               2025-table transplant from build_faithful_transplant.py); skip the
               h5py author step, just run the 6.x chain + report vs baseline.
  identity  -- h5py-rewrite the 2D tables with byte-identical values. If the
               solve reproduces the shipped baseline (maxWSE 951.93 ft, ~4881
               wet cells, vol err ~0.006%), the external-writer transplant path
               is sound (no corruption, no hidden RASMapper-provenance gate).
  perturb   -- h5py-rewrite the CELL VOLUME column scaled by TRANSPLANT_FACTOR
               (default 1.10). The solve MUST move (more cell storage attenuates
               the peak / changes wet extent) -- proving RasUnsteady genuinely
               CONSUMES the externally-authored tables (not a cached original).

Run inside trid3nt-local/hecras:latest with the venv python:
  docker run --rm -e TRANSPLANT_MODE=identity -v <this dir>:/t:ro --entrypoint bash \
    trid3nt-local/hecras:latest -lc '/opt/trid3nt/.venv/bin/python /t/transplant_solve.py'
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
import h5py, numpy as np

WRK = Path(os.environ.get("TRANSPLANT_WRK",
    "/opt/trid3nt/workers/hecras/fixtures/muncie_smoke/wrk_source"))
BIN = Path(os.environ.get("TRID3NT_HECRAS_BIN_DIR", "/opt/hecras/bin"))
LIBS = Path("/opt/hecras/libs")
PLAN = "Muncie.p04.tmp.hdf"
GEOM = "x04"
AREA = "/Geometry/2D Flow Areas/2D Interior Area"
MODE = os.environ.get("TRANSPLANT_MODE", "identity")
FACTOR = float(os.environ.get("TRANSPLANT_FACTOR", "1.10"))
_FILL = 1e30

# Baseline (/ on-machine 2026-08-04): the ground truth to reproduce.
BASE = {"wse_max_ft": 951.93, "wet_cells": 4881, "vol_err_pct": 0.00584}


def _env():
    e = dict(os.environ)
    e["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(LIBS), str(LIBS/"mkl"), str(LIBS/"rhel_8"), e.get("LD_LIBRARY_PATH","")])
    e["PATH"] = os.pathsep.join([str(BIN), e.get("PATH","")])
    return e


def _run(engine, cwd):
    p = subprocess.run([str(BIN/engine), PLAN, GEOM], cwd=str(cwd), env=_env(),
                       capture_output=True, text=True, timeout=3600)
    fin = "Finished" in p.stdout
    print(f"  $ {engine} -> exit={p.returncode} finished={fin}")
    if p.returncode != 0 or not fin:
        print(p.stdout[-1500:]); print(p.stderr[-800:], file=sys.stderr)
        raise SystemExit(f"FAIL: {engine} did not finish cleanly")


def author_tables(plan_path: Path):
    """The transplant WRITE: h5py-author the 2D subgrid tables in place."""
    with h5py.File(plan_path, "r+") as f:
        g = f[AREA]
        cve = g["Cells Volume Elevation Values"]   # (M,2) [Elevation, Volume]
        vals = cve[()].astype(np.float32)
        if MODE == "identity":
            cve[...] = vals  # external re-author, byte-identical
            note = "identity (byte-identical external re-author)"
        elif MODE == "perturb":
            v2 = vals.copy()
            v2[:, 1] = v2[:, 1] * FACTOR  # scale the VOLUME column only
            cve[...] = v2
            note = f"perturb cell-volume x{FACTOR}"
        elif MODE == "perturb_face":
            fae = g["Faces Area Elevation Values"]     # (K,4) [Z, Area, WettedPerim, Mann]
            fv = fae[()].astype(np.float32)
            fv[:, 1] = fv[:, 1] * FACTOR               # scale face AREA (conveyance)
            fae[...] = fv
            note = f"perturb face-area (conveyance) x{FACTOR}"
        else:
            raise SystemExit(f"unknown TRANSPLANT_MODE={MODE}")
        # Re-author the other 2D subgrid datasets identically (prove the whole
        # ragged Info+Values set is externally writable + consumed).
        for name in ("Cells Volume Elevation Info", "Faces Area Elevation Info",
                     "Faces Area Elevation Values", "Cells Minimum Elevation",
                     "Faces Minimum Elevation"):
            d = g[name]; d[...] = d[()]
        print(f"  [author] {note}; rewrote 2D Cells/Faces subgrid tables via h5py")


def metrics(plan_path: Path) -> dict:
    with h5py.File(plan_path, "r") as f:
        if "Results" not in f:
            raise SystemExit("FAIL: no /Results group")
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
        wse = a[:n]; mn = minel[:n]
        wet = int(np.nansum((wse - mn) > 0.01))
        return {"wse_max_ft": float(np.nanmax(wse)),
                "wse_min_ft": float(np.nanmin(wse)),
                "wet_cells": wet, "vol_err_pct": err,
                "flux_in": float(va.attrs["Total Boundary Flux of Water In"]),
                "flux_out": float(va.attrs["Total Boundary Flux of Water Out"])}


def cmp_tables(pristine: Path, post: Path):
    paths = ["Cells Volume Elevation Values", "Faces Area Elevation Values",
             "Cells Minimum Elevation", "Faces Minimum Elevation"]
    out = {}
    with h5py.File(pristine, "r") as fp, h5py.File(post, "r") as fq:
        for p in paths:
            a = np.asarray(fp[f"{AREA}/{p}"][()], np.float64)
            b = np.asarray(fq[f"{AREA}/{p}"][()], np.float64)
            out[p] = float(np.nanmax(np.abs(a - b)))
    return out


def main():
    rd = Path(tempfile.mkdtemp(prefix="transplant_"))
    for f in WRK.glob("*.*"):
        shutil.copy2(f, rd / f.name)
    print(f"=== TRANSPLANT SOLVE  mode={MODE}  factor={FACTOR if MODE=='perturb' else '-'} ===")
    if MODE != "prebuilt":
        author_tables(rd / PLAN)
    else:
        print("  [author] prebuilt deck (faithful 2025-table transplant) -- solve as-is")
    print("[1] RasGeomPreprocess (preserves 2D tables; rebuilds 1D only)")
    _run("RasGeomPreprocess", rd)
    if MODE != "prebuilt":
        tdiff = cmp_tables(WRK / PLAN, rd / PLAN)
        print(f"  [tables vs pristine after geompre] max|diff|: " +
              ", ".join(f"{k.split()[0]}={v:g}" for k, v in tdiff.items()))
    print("[2] RasUnsteady (2D subgrid solve reads the transplanted tables)")
    _run("RasUnsteady", rd)
    m = metrics(rd / PLAN)
    print("\n=== RESULT ===")
    print(f"  maxWSE={m['wse_max_ft']:.3f} ft   wet_cells={m['wet_cells']}   "
          f"vol_err={m['vol_err_pct']:.6f}%   flux in/out={m['flux_in']:.1f}/{m['flux_out']:.1f}")
    print(f"  baseline    maxWSE={BASE['wse_max_ft']} ft  wet~{BASE['wet_cells']}  "
          f"vol_err~{BASE['vol_err_pct']}%")
    if MODE in ("identity", "prebuilt"):
        dwse = abs(m["wse_max_ft"] - BASE["wse_max_ft"])
        ok = dwse < 0.05 and m["vol_err_pct"] < 0.05
        tag = "identity round-trip" if MODE == "identity" else "faithful 2025-table transplant"
        print(f"  VERDICT: {tag} {'REPRODUCES' if ok else 'DIVERGES from'} "
              f"baseline (dWSE={dwse:.3f} ft, wet={m['wet_cells']} vs ~{BASE['wet_cells']})")
    else:
        print(f"  VERDICT: perturbed tables -> solve moved (compare to identity run)")
    (Path(os.environ.get("TRANSPLANT_OUT", str(rd))) / "result.json").write_text(json.dumps(m, indent=2))
    shutil.rmtree(rd, ignore_errors=True)


if __name__ == "__main__":
    main()
