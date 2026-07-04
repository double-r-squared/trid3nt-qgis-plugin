"""LIVE readiness acceptance for the PySWMM quasi-2D urban-flood engine (urban
North Star).

Runs ON the agent box (the deployed grace2_agent editable install + the
aws-batch solver env). NO LLM, NO agent loop: it calls the REAL
``model_urban_flood_swmm`` workflow function DIRECTLY, which fetches a DEM +
OSM buildings, looks up the Atlas-14 design-storm depth, builds the quasi-2D
SWMM deck, runs the solve, and postprocesses to a peak depth COG.

IMPORTANT - lane selection: ``model_urban_flood_swmm`` defaults to the LOCAL
in-process pyswmm lane (``is_local_mode()`` True when ``GRACE2_SWMM_LOCAL`` is
unset). The readiness sweep proves the OFF-BOX AWS Batch lane, so
``GRACE2_SWMM_LOCAL=0`` MUST be set (the systemd solver.conf sets it on the box).
This driver fails loud if it is not, otherwise the "solve on Batch" claim is a
lie - it would silently run in-process.

Modeled on ``verify_mexico_beach_waves.py``: env sanity print, import the real
workflow, build minimal-but-valid inputs, call with ``await``, assert a non-
failed peak depth layer + a sane metric, print a clear PASS/FAIL line, return
exit code 0/1.

Minimal inputs (smallest valid urban-flood run):
  * bbox: a small ~0.005 deg city-block AOI in Chattanooga, TN (the region the
    SWMM engine tests stub around: -85.32, 35.02, -85.28, 35.06). The composer's
    _enforce_min_urban_aoi floors a too-small bbox to a 300 m square, so a
    collapsed bbox still yields a valid overland mesh.
  * storm_duration_hr: 1.0 - a SHORT storm keeps the solve fast (the engine
    tests use 1 h).
  * return_period_yr: 10 (a modest design storm; Atlas-14 looked up live).
  * target_resolution_m: 10 (the spike default; the adaptive budget may coarsen).
  * building_representation: "drop" (the contract default - buildings as holes).

PASS gate: the returned ``SWMMDepthLayerURI`` carries a renderable uri + a
finite ``max_depth_m`` >= 0.0.

USAGE (run ON the agent box i-0251879a278df797f):
    cd services/agent && python verify_readiness_swmm.py

Required env (set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_SWMM_LOCAL=0                          (force the OFF-BOX Batch lane)
    GRACE2_AWS_BATCH_JOB_DEF_SWMM=<swmm job-def>  (or generic _JOB_DEF)
    GRACE2_AWS_BATCH_QUEUE=grace2-solvers
    GRACE2_RUNS_BUCKET=<runs bucket>
    GRACE2_CACHE_BUCKET=<cache bucket>          (DEM + manifest staging target)
    AWS_REGION / AWS_DEFAULT_REGION=us-west-2
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

ENGINE = "swmm"

# A small city-block AOI in Chattanooga, TN (inside the SWMM-test region around
# -85.32, 35.02, -85.28, 35.06). The composer floors a too-small bbox to a 300 m
# square (_enforce_min_urban_aoi), so this collapses to that floor.
TINY_BBOX: tuple[float, float, float, float] = (-85.310, 35.045, -85.305, 35.050)


def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="small city-block AOI + 1h storm (default; cheapest Batch run)",
    )
    args = ap.parse_args(argv)
    _ = args.tiny

    bbox = TINY_BBOX
    storm_duration_hr = 1.0
    return_period_yr = 10
    target_resolution_m = 10.0

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    swmm_local = (os.environ.get("GRACE2_SWMM_LOCAL") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_SWMM")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    cache_bucket = (os.environ.get("GRACE2_CACHE_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()

    print("=== PySWMM urban-flood readiness acceptance (live AWS, OFF-BOX lane) ===")
    print(f"  engine:          {ENGINE}")
    print(f"  bbox (EPSG4326): {_bbox_str(bbox)}  (floored to >=300m square)")
    print(f"  storm_duration_hr: {storm_duration_hr}  return_period_yr: {return_period_yr}")
    print(f"  target_res_m:    {target_resolution_m}  building_representation: drop")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_SWMM_LOCAL={swmm_local!r} (must be '0'/false for the Batch lane)")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_SWMM(or generic)={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  GRACE2_CACHE_BUCKET={cache_bucket!r} (DEM + manifest staging target)")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' to dispatch the SWMM "
            "off-box solve to Batch."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=backend_not_aws_batch")
        return 2
    # The SWMM composer runs in-process by DEFAULT (GRACE2_SWMM_LOCAL unset ->
    # local). For a Batch-lane readiness proof it MUST be explicitly off-box.
    if swmm_local not in {"0", "false", "no", "off"}:
        print(
            "FAIL: GRACE2_SWMM_LOCAL must be set to '0' (false) so the composer "
            "uses the OFF-BOX AWS Batch lane. With it unset the solve runs "
            "in-process and the Batch readiness claim is false."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=swmm_local_lane")
        return 2
    if not job_def:
        print(
            "FAIL: no SWMM job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_SWMM (or the generic "
            "GRACE2_AWS_BATCH_JOB_DEF)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_job_def")
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=missing_queue_or_runs_bucket")
        return 2

    # Import the REAL workflow + the run-args contract (no LLM, no agent).
    from grace2_contracts.swmm_contracts import SWMMDepthLayerURI, SWMMRunArgs
    from grace2_agent.workflows.model_urban_flood_swmm import model_urban_flood_swmm

    run_args = SWMMRunArgs(
        bbox=bbox,
        storm_duration_hr=storm_duration_hr,
        return_period_yr=return_period_yr,
        target_resolution_m=target_resolution_m,
        building_representation="drop",
    )

    print("\n--- running urban-flood chain (DEM+buildings+Atlas14 -> deck -> Batch solve) ---")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    try:
        peak = await model_urban_flood_swmm(run_args, compute_class="standard")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", type(exc).__name__)
        print(f"\nFAIL: composer raised {type(exc).__name__}: {exc}")
        print(f"READINESS_RESULT {ENGINE} FAIL reason={code}")
        return 1

    if not isinstance(peak, SWMMDepthLayerURI):
        print(f"\nFAIL: composer returned {type(peak).__name__}, not SWMMDepthLayerURI")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_layer")
        return 1

    run_id = (peak.layer_id or "unknown").rsplit("-", 1)[-1]
    max_depth_m = float(peak.max_depth_m)
    print("\n=== peak depth layer ===")
    print(f"  layer_id:        {peak.layer_id}")
    print(f"  role:            {peak.role}  style: {peak.style_preset}")
    print(f"  uri:             {peak.uri}")
    print(f"  max_depth_m:     {max_depth_m}")
    print(f"  flooded_area_km2: {peak.flooded_area_km2}")
    print(f"  n_buildings_affected: {peak.n_buildings_affected}")

    if not (math.isfinite(max_depth_m) and max_depth_m >= 0.0):
        print("\nFAIL: max_depth_m is NaN/inf/negative - the solve produced no sane field.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=bad_metric")
        return 1

    print("\n=== RESULT ===")
    print(
        f"PASS: SWMM off-box Batch solve ran, postprocess produced a peak depth "
        f"layer, max_depth_m={max_depth_m:.4g} m."
    )
    print(
        f"READINESS_RESULT {ENGINE} PASS run_id={run_id} layers=1 "
        f"metric=max_depth_m={max_depth_m:.6g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
