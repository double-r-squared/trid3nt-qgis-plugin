"""Muncie replication gate driver (mesh wave M3 -- the wave's HARD acceptance gate).

Runs HEC's shipped Muncie test project (White River, Muncie IN) through the 6.6
Linux computation engines and verifies the geometry pipeline against the
GUI-computed baseline. Runnable BOTH on the host (point TRID3NT_HECRAS_ROOT at an
extracted Linux_RAS_v66/ dir) and in-container (defaults to /opt/hecras).

Two comparison bases, documented with tolerances:

  (A) HYDRAULIC PROPERTY TABLES -- pristine GUI-computed (wrk_source/, untouched)
      vs Linux RasGeomPreprocess-recomputed. HEC's release notes state the plan
      HDF already carries the GUI-computed hydraulic properties; RasGeomPreprocess
      rebuilds them from the x04 geometry. Since the geometry is unchanged, the
      Linux-recomputed tables must reproduce the GUI tables EXACTLY.
      Gate: max|diff| == 0 on the 1D XSEC Value + 2D Cells Volume-Elevation +
      2D Faces Area-Elevation tables (verified 2026-08-03: all zero).

  (B) VOLUME ACCOUNTING -- RasUnsteady's mass-balance error. The community Docker
      repro (neeraip/hecras-v66-linux) reports Muncie at 0.00% volume error /
      0.001 ft WSE vs the Windows GUI. Gate: |Error Percent| < 0.05%
      (observed 0.005835%).

Exit 0 = both gates green; nonzero = a gate failed (honest, no silent pass).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np

HERE = Path(__file__).resolve().parent
WRK_SOURCE = HERE / "wrk_source"
RAS_ROOT = Path(os.environ.get("TRID3NT_HECRAS_ROOT", "/opt/hecras"))

PLAN_HDF = "Muncie.p04.tmp.hdf"
GEOM_SUFFIX = "x04"
VOL_ERR_TOL_PCT = 0.05

# Property-table datasets that must be bit-identical GUI vs Linux-preprocessed.
PROP_TABLE_PATHS = [
    "Geometry/Cross Sections/Property Tables/XSEC Value",
    "Geometry/Cross Sections/Property Tables/Cell Value",
    "Geometry/2D Flow Areas/2D Interior Area/Cells Volume Elevation Values",
    "Geometry/2D Flow Areas/2D Interior Area/Faces Area Elevation Values",
    "Geometry/2D Flow Areas/2D Interior Area/Cells Minimum Elevation",
    "Geometry/2D Flow Areas/2D Interior Area/Faces Minimum Elevation",
]


def _env() -> dict:
    e = dict(os.environ)
    libs = RAS_ROOT / "libs"
    e["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(libs), str(libs / "mkl"), str(libs / "rhel_8"), e.get("LD_LIBRARY_PATH", "")]
    )
    e["PATH"] = os.pathsep.join([str(RAS_ROOT / "bin"), e.get("PATH", "")])
    return e


def _run(engine: str, run_dir: Path) -> None:
    cmd = [str(RAS_ROOT / "bin" / engine), PLAN_HDF, GEOM_SUFFIX]
    print(f"  $ {engine} {PLAN_HDF} {GEOM_SUFFIX}")
    p = subprocess.run(cmd, cwd=str(run_dir), env=_env(), capture_output=True, text=True, timeout=3600)
    if p.returncode != 0 or "Finished" not in p.stdout:
        print(p.stdout[-2000:])
        print(p.stderr[-1000:], file=sys.stderr)
        raise SystemExit(f"FAIL: {engine} did not finish cleanly (exit {p.returncode})")
    print(f"    -> {[l for l in p.stdout.splitlines() if 'Finished' in l][-1].strip()}")


def _cmp_property_tables(pristine: Path, post: Path) -> bool:
    ok = True
    with h5py.File(pristine, "r") as fp, h5py.File(post, "r") as fq:
        for path in PROP_TABLE_PATHS:
            if path not in fp or path not in fq:
                print(f"  [MISS] {path}: pristine={path in fp} post={path in fq}")
                ok = False
                continue
            a = np.asarray(fp[path][()], dtype=np.float64)
            b = np.asarray(fq[path][()], dtype=np.float64)
            if a.shape != b.shape:
                print(f"  [SHAPE] {path}: {a.shape} vs {b.shape}")
                ok = False
                continue
            mx = float(np.nanmax(np.abs(a - b)))
            flag = "OK " if mx == 0.0 else "DIFF"
            print(f"  [{flag}] {path.split('/')[-1]}: max|diff|={mx:g}  shape={a.shape}")
            ok = ok and (mx == 0.0)
    return ok


def _volume_accounting(post: Path) -> dict:
    with h5py.File(post, "r") as f:
        if "Results" not in f:
            raise SystemExit("FAIL: no /Results group after RasUnsteady")
        va = f["Results/Unsteady/Summary/Volume Accounting"]
        return {k: va.attrs[k] for k in va.attrs}


def main() -> int:
    if not (RAS_ROOT / "bin" / "RasGeomPreprocess").is_file():
        raise SystemExit(
            f"FAIL: HEC-RAS engines not found under {RAS_ROOT}. Set TRID3NT_HECRAS_ROOT "
            f"to an extracted Linux_RAS_v66/ dir (or run inside the hecras worker image)."
        )
    print("=" * 72)
    print("MUNCIE REPLICATION GATE (mesh wave M3) -- White River, Muncie IN")
    print(f"  engines: {RAS_ROOT}/bin   fixture: {WRK_SOURCE}")
    print("=" * 72)

    run_dir = Path(tempfile.mkdtemp(prefix="muncie_gate_"))
    try:
        for f in WRK_SOURCE.glob("*.*"):
            shutil.copy2(f, run_dir / f.name)

        print("\n[1] RasGeomPreprocess (rebuild hydraulic property tables from x04 geometry)")
        _run("RasGeomPreprocess", run_dir)

        print("\n[2] GATE A -- property tables: GUI-computed vs Linux-recomputed")
        gate_a = _cmp_property_tables(WRK_SOURCE / PLAN_HDF, run_dir / PLAN_HDF)

        print("\n[3] RasUnsteady (unsteady solve -> Results)")
        _run("RasUnsteady", run_dir)

        print("\n[4] GATE B -- volume accounting mass balance")
        va = _volume_accounting(run_dir / PLAN_HDF)
        err_pct = abs(float(va["Error Percent"]))
        gate_b = err_pct < VOL_ERR_TOL_PCT
        print(f"  Error Percent = {float(va['Error Percent']):.6f}%  (tol < {VOL_ERR_TOL_PCT}%)  "
              f"[{'OK' if gate_b else 'FAIL'}]")
        units = va.get("Vol Accounting in", b"Acre Feet")
        units = units.decode() if isinstance(units, bytes) else units
        print(f"  Error = {float(va['Error']):.4f} {units}")
        print(f"  Boundary flux in/out = {float(va['Total Boundary Flux of Water In']):.1f} / "
              f"{float(va['Total Boundary Flux of Water Out']):.1f}")

        print("\n" + "=" * 72)
        verdict = gate_a and gate_b
        print(f"MUNCIE GATE: {'GREEN' if verdict else 'RED'}  "
              f"(property tables {'identical' if gate_a else 'DIVERGED'}, "
              f"volume error {err_pct:.6f}% {'within' if gate_b else 'EXCEEDS'} tol)")
        print("=" * 72)
        return 0 if verdict else 1
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
