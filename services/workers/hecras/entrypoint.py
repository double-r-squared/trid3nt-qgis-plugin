"""HEC-RAS 6.x Linux worker entrypoint (mesh wave M3 / hecras_geometry gate).

Runs the headless geometry-preprocess -> unsteady-solve pipeline HEC's own
Linux computation engines expose, over a bind-mounted rundir. The agent-side
launcher mounts a rundir at ``/data`` (``docker run ... -v <rundir>:/data``)
carrying the HEC-RAS deck files + ``manifest.json``; this shim:

  1. optionally runs ``RasGeomPreprocess <plan_hdf> <geom_suffix>`` -- rebuilds
     the hydraulic property tables (cell volume-elevation, face area-elevation,
     1D cross-section conveyance) on the plan HDF from the geometry;
  2. runs ``RasUnsteady <plan_hdf> <geom_suffix>`` -- the unsteady solve, which
     appends a ``Results`` group to the plan HDF;
  3. extracts the volume-accounting summary + max water-surface from the plan
     HDF via h5py and writes ``hecras_metrics.json`` back into the rundir.

The launcher uploads ``/data`` and writes completion.json, so this image does
NO object-store I/O (mirror of the telemac local worker). Honest failure: a
nonzero engine exit, a missing "Finished" sentinel, or an absent Results group
raises -- never a silent success.

This is the M3 mesh-wave gate (prove the geometry pipeline on Muncie), NOT the
HEC-RAS engine landing: there is no registered tool / template / contract
archetype here yet. The ras-commander ``Hdf*`` readers ride in the image for the
engine-landing wave; the metric extraction below stays pure-h5py so the gate
does not depend on the heavy geo closure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np

# HEC's run scripts set LD_LIBRARY_PATH to libs : libs/mkl : libs/rhel_8 and put
# the engines on PATH; the Dockerfile bakes both env vars, so a bare engine name
# resolves. TRID3NT_HECRAS_BIN_DIR is the fallback if PATH was not inherited.
BIN_DIR = os.environ.get("TRID3NT_HECRAS_BIN_DIR", "/opt/hecras/bin")
DATA_DIR = os.environ.get("TRID3NT_HECRAS_DATA_DIR", "/data")

# HDF sentinel for a "no data" cell/face (HEC-RAS writes a large positive fill).
_HDF_FILL = 1e30


class HecrasError(RuntimeError):
    """A HEC-RAS engine leg failed or produced no usable result."""


def _engine(name: str) -> str:
    """Absolute path to a bundled engine, preferring PATH then the bin dir."""
    direct = Path(BIN_DIR) / name
    if direct.is_file():
        return str(direct)
    return name  # rely on PATH (Dockerfile bakes /opt/hecras/bin)


def _run_engine(name: str, plan_hdf: str, geom_suffix: str, cwd: Path) -> None:
    """Run one engine leg, streaming output, and assert a clean finish.

    The engines return 0 and print a ``Finished ...`` line on success. We assert
    BOTH the exit code AND the sentinel so a mid-run abort (which can still exit
    0 after printing an error) is caught honestly.
    """
    cmd = [_engine(name), plan_hdf, geom_suffix]
    print(f"[hecras] running: {' '.join(cmd)}  (cwd={cwd})", flush=True)
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=3600
    )
    tail = "\n".join(proc.stdout.splitlines()[-12:])
    print(f"[hecras] {name} exit={proc.returncode}\n{tail}", flush=True)
    if proc.returncode != 0:
        raise HecrasError(
            f"{name} exited {proc.returncode}\nstderr:\n{proc.stderr[-2000:]}"
        )
    if "Finished" not in proc.stdout:
        raise HecrasError(
            f"{name} exited 0 but printed no 'Finished' sentinel -- treating as "
            f"a failed run.\nstdout tail:\n{tail}"
        )


def _finite(arr: np.ndarray) -> np.ndarray:
    """Mask HEC-RAS fill values to NaN so summaries ignore dry/no-data cells."""
    a = np.asarray(arr, dtype=np.float64)
    return np.where(np.abs(a) > _HDF_FILL, np.nan, a)


def _extract_metrics(plan_hdf: Path) -> dict:
    """Pull volume accounting + max WSE from the results-bearing plan HDF.

    Raises if no ``Results`` group is present (an unsteady solve that wrote no
    results is a failure, not an empty success).
    """
    with h5py.File(plan_hdf, "r") as f:
        if "Results" not in f:
            raise HecrasError(
                f"{plan_hdf.name} has no /Results group after the unsteady run"
            )
        va = f["Results/Unsteady/Summary/Volume Accounting"]
        metrics: dict = {
            "volume_accounting": {
                k: (
                    va.attrs[k].decode()
                    if isinstance(va.attrs[k], bytes)
                    else float(va.attrs[k])
                    if np.isscalar(va.attrs[k]) and not isinstance(va.attrs[k], bytes)
                    else va.attrs[k].tolist()
                )
                for k in va.attrs
            }
        }
        # 2D flow-area max water surface (headline for the coastal/riverine 2D
        # result) -- present only when the deck has a 2D flow area.
        base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"
        two_d = f.get(f"{base}/2D Flow Areas")
        if two_d is not None:
            for area_name in two_d:
                mw = two_d[f"{area_name}/Maximum Water Surface"]
                a = _finite(mw[()])
                metrics.setdefault("max_water_surface_2d", {})[area_name] = {
                    "cells": int(a.shape[-1]),
                    "min_ft": float(np.nanmin(a)),
                    "max_ft": float(np.nanmax(a)),
                }
        xs = f.get(f"{base}/Cross Sections/Maximum Water Surface")
        if xs is not None:
            # Shape is (n_profiles, n_xs); profile row 0 is the max WSE profile
            # (later rows carry other summary quantities in HEC's flow units, so a
            # global max would misreport a discharge as a water surface).
            a = _finite(xs[()])
            wse = a[0] if a.ndim == 2 else a
            metrics["max_water_surface_1d"] = {
                "cross_sections": int(a.shape[-1]),
                "min_ft": float(np.nanmin(wse)),
                "max_ft": float(np.nanmax(wse)),
            }
    return metrics


def run(data_dir: Path) -> dict:
    """Execute the manifest's HEC-RAS legs and return the metrics dict."""
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        raise HecrasError(f"no manifest.json in {data_dir}")
    manifest = json.loads(manifest_path.read_text())

    plan_hdf = manifest["plan_hdf"]  # e.g. "Muncie.p04.tmp.hdf"
    geom_suffix = manifest["geom_suffix"]  # e.g. "x04"
    run_geompre = bool(manifest.get("run_geompre", True))

    if not (data_dir / plan_hdf).is_file():
        raise HecrasError(f"plan HDF {plan_hdf} not found in {data_dir}")

    if run_geompre:
        _run_engine("RasGeomPreprocess", plan_hdf, geom_suffix, data_dir)
    _run_engine("RasUnsteady", plan_hdf, geom_suffix, data_dir)

    metrics = _extract_metrics(data_dir / plan_hdf)
    metrics["plan_hdf"] = plan_hdf
    metrics["ran_geompre"] = run_geompre
    (data_dir / "hecras_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(f"[hecras] wrote hecras_metrics.json", flush=True)
    return metrics


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = Path(argv[0]) if argv else Path(DATA_DIR)
    try:
        metrics = run(data_dir)
    except Exception as exc:  # honest surface: non-zero exit + the reason
        print(f"[hecras] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    err_pct = metrics["volume_accounting"].get("Error Percent")
    print(f"[hecras] DONE -- volume accounting error {err_pct}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
