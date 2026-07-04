"""LIVE readiness acceptance for the Landlab landslide-susceptibility engine.

Runs ON the agent box (the deployed grace2_agent editable install + the
aws-batch solver env). NO LLM, NO agent loop: it calls the REAL
``model_landslide_scenario`` workflow function DIRECTLY, which fetches a DEM,
stages a manifest, submits the ``landlab`` AWS Batch job, waits, downloads the
field COG, and postprocesses it into a ``LandlabSusceptibilityLayerURI``.

Modeled on ``verify_mexico_beach_waves.py``: env sanity print, import the real
workflow, build minimal-but-valid inputs, call with ``await``, assert a non-
failed susceptibility layer + a sane metric, print a clear PASS/FAIL line,
return exit code 0/1.

Minimal inputs (smallest valid landslide-susceptibility run):
  * bbox: a tiny ~0.01 deg hillslope AOI near Portland, OR (the region pinned in
    test_landlab_engine.py: (-122.5, 45.4, -122.4, 45.5)), shrunk hard. The
    composer's _enforce_min_landslide_aoi floors it to a 500 m square, so a
    collapsed bbox still yields a valid hillslope grid.
  * analysis: "landslide_probability" (the contract default; the infinite-slope
    LandslideProbability chain).
  * target_resolution_m: 30 m (the demo default; a hillslope-scale grid).
  * n_monte_carlo: 25 - LOW on purpose (vs the 250 demo default) so the
    Monte-Carlo probability-of-failure field is cheap to compute.

PASS gate: the returned ``LandlabSusceptibilityLayerURI`` carries a renderable
uri + a finite ``unstable_area_fraction`` in [0, 1].

USAGE (run ON the agent box i-0251879a278df797f):
    cd services/agent && python verify_readiness_landlab.py

Required env (set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_AWS_BATCH_JOB_DEF_LANDLAB=<landlab job-def>   (or generic _JOB_DEF)
    GRACE2_AWS_BATCH_QUEUE=grace2-solvers
    GRACE2_RUNS_BUCKET=<runs bucket>
    GRACE2_CACHE_BUCKET=<cache bucket>          (manifest + DEM staging target)
    AWS_REGION / AWS_DEFAULT_REGION=us-west-2
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

ENGINE = "landlab"

# A tiny hillslope AOI near Portland, OR (inside the test region
# (-122.5, 45.4, -122.4, 45.5)). The composer floors a too-small bbox to a
# 500 m square (_enforce_min_landslide_aoi), so this collapses to that floor.
TINY_BBOX: tuple[float, float, float, float] = (-122.45, 45.45, -122.44, 45.46)


def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="tiny hillslope AOI + low Monte-Carlo (default; cheapest Batch run)",
    )
    args = ap.parse_args(argv)
    _ = args.tiny

    bbox = TINY_BBOX
    analysis = "landslide_probability"
    target_resolution_m = 30.0
    n_monte_carlo = 25

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_LANDLAB")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    cache_bucket = (os.environ.get("GRACE2_CACHE_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()

    print("=== Landlab landslide-susceptibility readiness acceptance (live AWS) ===")
    print(f"  engine:          {ENGINE}")
    print(f"  bbox (EPSG4326): {_bbox_str(bbox)}  (floored to >=500m square)")
    print(f"  analysis:        {analysis}")
    print(f"  target_res_m:    {target_resolution_m}  n_monte_carlo: {n_monte_carlo}")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_LANDLAB(or generic)={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  GRACE2_CACHE_BUCKET={cache_bucket!r} (manifest + DEM staging target)")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' (Landlab runs OFF-BOX "
            "only - the scale-to-zero island norm; there is no in-process lane)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=backend_not_aws_batch")
        return 2
    if not job_def:
        print(
            "FAIL: no Landlab job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_LANDLAB (or the generic "
            "GRACE2_AWS_BATCH_JOB_DEF)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_job_def")
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=missing_queue_or_runs_bucket")
        return 2

    # Import the REAL workflow + the run-args contract (no LLM, no agent).
    from grace2_contracts.landlab_contracts import (
        LandlabRunArgs,
        LandlabSusceptibilityLayerURI,
    )
    from grace2_agent.workflows.model_landslide_scenario import (
        model_landslide_scenario,
    )

    run_args = LandlabRunArgs(
        bbox=bbox,
        analysis=analysis,
        target_resolution_m=target_resolution_m,
        n_monte_carlo=n_monte_carlo,
    )

    print("\n--- submitting landlab Batch job (DEM fetch -> stage -> solve) ---")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    try:
        primary = await model_landslide_scenario(
            run_args, compute_class="standard"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", type(exc).__name__)
        print(f"\nFAIL: composer raised {type(exc).__name__}: {exc}")
        print(f"READINESS_RESULT {ENGINE} FAIL reason={code}")
        return 1

    if not isinstance(primary, LandlabSusceptibilityLayerURI):
        print(
            f"\nFAIL: composer returned {type(primary).__name__}, not "
            "LandlabSusceptibilityLayerURI"
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_layer")
        return 1

    run_id = (primary.layer_id or "unknown").rsplit("-", 1)[-1]
    unstable_frac = float(primary.unstable_area_fraction)
    print("\n=== susceptibility layer ===")
    print(f"  layer_id:        {primary.layer_id}")
    print(f"  role:            {primary.role}  style: {primary.style_preset}")
    print(f"  uri:             {primary.uri}")
    print(f"  unstable_area_fraction:    {unstable_frac}")
    print(f"  min_factor_of_safety:      {primary.min_factor_of_safety}")
    print(f"  mean_probability_of_failure: {primary.mean_probability_of_failure}")

    if not (math.isfinite(unstable_frac) and 0.0 <= unstable_frac <= 1.0):
        print("\nFAIL: unstable_area_fraction is NaN/out-of-range - no sane field.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=bad_metric")
        return 1

    print("\n=== RESULT ===")
    print(
        f"PASS: Landlab Batch solve ran, postprocess produced a susceptibility "
        f"layer, unstable_area_fraction={unstable_frac:.4g}."
    )
    print(
        f"READINESS_RESULT {ENGINE} PASS run_id={run_id} layers=1 "
        f"metric=unstable_area_fraction={unstable_frac:.6g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
