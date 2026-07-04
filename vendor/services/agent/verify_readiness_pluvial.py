"""LIVE readiness acceptance for the SFINCS PLUVIAL flood path (Idaho/pluvial
North Star) - the most-touched code path in the agent.

Runs ON the agent box (the deployed grace2_agent editable install + the
aws-batch solver env). NO LLM, NO agent loop: it calls the REAL
``model_flood_scenario`` workflow function DIRECTLY in PLUVIAL mode
(``quadtree=False, coastal=False, surge_forcing=None``), which fetches a 3DEP DEM
+ NLCD landcover + river geometry + the Atlas-14 design-storm depth, builds a
regular-grid SFINCS deck, submits the ``sfincs`` AWS Batch job, waits, and
postprocesses the NetCDF into a flood-depth COG carried on an AssessmentEnvelope.

This re-confirms the most-exercised pipeline (the regular-grid pluvial SFINCS
path every flood demo rides) end-to-end on Batch. It is the pluvial counterpart
to ``verify_mexico_beach_waves.py`` (which proves the quadtree+SnapWave coastal
path); together they cover both halves of model_flood_scenario.

Modeled on ``verify_mexico_beach_waves.py``: env sanity print, import the real
workflow, build minimal-but-valid inputs, call with ``await``, assert a non-
failed envelope with >= 1 layer + a sane metric, print a clear PASS/FAIL line,
return exit code 0/1.

Minimal inputs (smallest valid pluvial run):
  * bbox: a small ~0.06 deg AOI near Boise, ID (the Idaho pluvial North Star
    region). Pluvial = rain-only, so an inland AOI with no coastline is correct.
  * return_period_yr: 100 (Atlas-14 design storm), duration_hr: 6 (short window
    keeps the solve cheap; Atlas-14 + SFINCS both accept it).
  * quadtree=False, coastal=False, surge_forcing=None -> the regular-grid pluvial
    branch (byte-identical to the v0.1 workflow; the regression-critical path).
  * building_obstacles=False -> no OSM footprint burn (keeps the run cheap).

PASS gate: a non-failed envelope (no ":FAILED:" in workflow_name AND a non-empty
solver_run_ids) with >= 1 layer AND a finite ``flood.metrics.max_depth_m`` >= 0.

USAGE (run ON the agent box i-0251879a278df797f):
    cd services/agent && python verify_readiness_pluvial.py

Required env (set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_AWS_BATCH_JOB_DEF_SFINCS=<sfincs job-def>   (or generic _JOB_DEF)
    GRACE2_AWS_BATCH_QUEUE=grace2-solvers
    GRACE2_RUNS_BUCKET=<runs bucket>
    GRACE2_CACHE_BUCKET=<cache bucket>
    AWS_REGION / AWS_DEFAULT_REGION=us-west-2
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

ENGINE = "pluvial"

# A small inland AOI near Boise, ID (the pluvial/Idaho North Star region). No
# coastline -> the coastal/surge branch is correctly NOT taken (pure pluvial).
TINY_BBOX: tuple[float, float, float, float] = (-116.25, 43.58, -116.19, 43.64)


def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="small inland AOI + 6h design storm (default; cheapest Batch run)",
    )
    args = ap.parse_args(argv)
    _ = args.tiny

    bbox = TINY_BBOX
    return_period_yr = 100
    duration_hr = 6

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_SFINCS")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()

    print("=== SFINCS PLUVIAL flood readiness acceptance (live AWS) ===")
    print(f"  engine:          {ENGINE} (model_flood_scenario regular-grid pluvial)")
    print(f"  bbox (EPSG4326): {_bbox_str(bbox)}")
    print(f"  return_period_yr: {return_period_yr}  duration_hr: {duration_hr}")
    print("  mode:            quadtree=False coastal=False surge_forcing=None")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_SFINCS(or generic)={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' to dispatch the "
            "pluvial SFINCS solve to Batch."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=backend_not_aws_batch")
        return 2
    if not job_def:
        print(
            "FAIL: no SFINCS job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_SFINCS (or the generic "
            "GRACE2_AWS_BATCH_JOB_DEF)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_job_def")
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=missing_queue_or_runs_bucket")
        return 2

    # Import the REAL workflow (no LLM, no agent).
    from grace2_agent.workflows.model_flood_scenario import model_flood_scenario

    print("\n--- running pluvial flood chain (DEM+NLCD+precip -> SFINCS Batch solve) ---")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    try:
        envelope = await model_flood_scenario(
            bbox=bbox,
            return_period_yr=return_period_yr,
            duration_hr=duration_hr,
            quadtree=False,
            coastal=False,
            surge_forcing=None,
            building_obstacles=False,
            compute_class="medium",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - model_flood_scenario is caller-friendly,
        # but guard against an unexpected raise.
        code = getattr(exc, "error_code", type(exc).__name__)
        print(f"\nFAIL: composer raised {type(exc).__name__}: {exc}")
        print(f"READINESS_RESULT {ENGINE} FAIL reason={code}")
        return 1

    workflow_name = getattr(envelope, "workflow_name", "")
    solver_run_ids = list(getattr(envelope, "solver_run_ids", []) or [])
    layers = list(getattr(envelope, "layers", []) or [])
    print("\n=== workflow envelope ===")
    print(f"  envelope_type: {getattr(envelope, 'envelope_type', None)}")
    print(f"  workflow_name: {workflow_name}")
    print(f"  solver_run_ids: {solver_run_ids}")
    print(f"  layer count:   {len(layers)}")
    for lyr in layers:
        print(
            f"    - id={getattr(lyr, 'layer_id', None)} "
            f"role={getattr(lyr, 'role', None)} "
            f"name={getattr(lyr, 'name', None)!r}"
        )

    # --- FAIL FAST on a failed envelope (the template's failed-shape check) -----
    # The partial-failure shape tags workflow_name as "<name>:FAILED:<CODE>" and
    # threads "failed:<CODE>" into the flood metrics. No solver_run_ids = no solve.
    if ":FAILED:" in workflow_name or not solver_run_ids:
        sv = None
        try:
            sv = envelope.flood.metrics.solver_version  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        print(
            f"\nFAIL: workflow returned a FAILED envelope "
            f"(workflow_name={workflow_name!r}, solver_version={sv!r})."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=failed_envelope")
        return 1

    if not layers:
        print("\nFAIL: envelope carries no layers (no flood-depth COG produced).")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_layer")
        return 1

    run_id = solver_run_ids[-1]
    max_depth_m = None
    try:
        max_depth_m = float(envelope.flood.metrics.max_depth_m)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        max_depth_m = None
    print(f"\n  run_id:       {run_id}")
    print(f"  max_depth_m:  {max_depth_m}")

    if max_depth_m is None or not (math.isfinite(max_depth_m) and max_depth_m >= 0.0):
        print("\nFAIL: flood.metrics.max_depth_m is missing/NaN/negative - no sane field.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=bad_metric")
        return 1

    print("\n=== RESULT ===")
    print(
        f"PASS: pluvial SFINCS Batch solve ran, postprocess produced "
        f"{len(layers)} layer(s), max_depth_m={max_depth_m:.4g} m."
    )
    print(
        f"READINESS_RESULT {ENGINE} PASS run_id={run_id} layers={len(layers)} "
        f"metric=max_depth_m={max_depth_m:.6g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
