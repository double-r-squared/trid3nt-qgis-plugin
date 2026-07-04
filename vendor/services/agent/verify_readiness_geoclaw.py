"""LIVE readiness acceptance for the GeoClaw (Clawpack) shallow-water engine.

Runs ON the agent box (the deployed grace2_agent editable install + the
aws-batch solver env from systemd solver.conf). NO LLM, NO agent loop: it calls
the REAL ``model_dambreak_geoclaw_scenario`` workflow function DIRECTLY, which
fetches a topo/bathy DEM, stages a build_spec, submits the ``geoclaw`` AWS Batch
job, waits, downloads the fort.q frames, and postprocesses to a peak depth COG.

Modeled on ``verify_mexico_beach_waves.py``: env sanity print, import the real
workflow, build minimal-but-valid inputs (the SMALLEST cheap AOI), call with
``await``, assert a non-failed result with >= 1 layer + a sane metric, print a
clear PASS/FAIL line, return exit code 0/1.

Minimal inputs (smallest valid GeoClaw dam-break run):
  * bbox: a tiny ~0.05 x 0.05 deg AOI in the Mexico Beach demo region (the
    region pinned in test_run_geoclaw_chain.py, shrunk hard for cheapness).
  * scenario: "dam_break" (the contract default; the cheapest driver - a raised
    water column released over dry topo, no dtopo / surge file required).
  * sim_duration_s: 600 s (10 min - short physical time keeps the solve cheap).
  * output_frames: 3 (enough for the peak + a couple of frames).
  * amr_levels: 1 (uniform base grid - no AMR refinement burn).

PASS gate: the returned ``GeoClawDepthLayerURI`` (>= 1 layer by construction)
carries a renderable uri + ``max_depth_m`` that is finite and >= 0.0.

USAGE (run ON the agent box i-0251879a278df797f):
    cd services/agent && python verify_readiness_geoclaw.py
    python verify_readiness_geoclaw.py --tiny   # explicit (default)

Required env (set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_AWS_BATCH_JOB_DEF_GEOCLAW=<geoclaw job-def>   (or generic _JOB_DEF)
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

ENGINE = "geoclaw"

# Tiny dam-break AOI: a ~0.05 x 0.05 deg box inside the Mexico Beach demo region
# (test_run_geoclaw_chain.py uses (-85.75, 29.55, -85.25, 30.20)); shrunk hard so
# the base grid + solve stay the cheapest valid GeoClaw run.
TINY_BBOX: tuple[float, float, float, float] = (-85.45, 29.92, -85.40, 29.97)


def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="tiny dam-break AOI + 600 s window (default; cheapest Batch run)",
    )
    args = ap.parse_args(argv)
    _ = args.tiny  # default-only; flag accepted for harness symmetry

    bbox = TINY_BBOX
    scenario = "dam_break"
    sim_duration_s = 600.0
    output_frames = 3
    amr_levels = 1

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_GEOCLAW")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()

    print("=== GeoClaw shallow-water readiness acceptance (live AWS) ===")
    print(f"  engine:          {ENGINE}")
    print(f"  bbox (EPSG4326): {_bbox_str(bbox)}")
    print(f"  scenario:        {scenario}")
    print(f"  sim_duration_s:  {sim_duration_s}")
    print(f"  output_frames:   {output_frames}  amr_levels: {amr_levels}")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_GEOCLAW(or generic)={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' (GeoClaw is "
            "Batch-only; the Clawpack Fortran lives in the worker image, never in "
            "the agent venv). Set the env (it is set on the agent box) and re-run."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=backend_not_aws_batch")
        return 2
    if not job_def:
        print(
            "FAIL: no GeoClaw job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_GEOCLAW (or the generic "
            "GRACE2_AWS_BATCH_JOB_DEF)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_job_def")
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=missing_queue_or_runs_bucket")
        return 2

    # Import the REAL workflow + the run-args contract (no LLM, no agent).
    from grace2_contracts.geoclaw_contracts import (
        GeoClawDepthLayerURI,
        GeoClawRunArgs,
    )
    from grace2_agent.workflows.model_dambreak_geoclaw_scenario import (
        model_dambreak_geoclaw_scenario,
    )

    run_args = GeoClawRunArgs(
        bbox=bbox,
        scenario=scenario,
        sim_duration_s=sim_duration_s,
        output_frames=output_frames,
        amr_levels=amr_levels,
    )

    print("\n--- submitting geoclaw Batch job (DEM fetch -> stage -> solve) ---")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    try:
        peak = await model_dambreak_geoclaw_scenario(
            run_args, compute_class="standard"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", type(exc).__name__)
        print(f"\nFAIL: composer raised {type(exc).__name__}: {exc}")
        print(f"READINESS_RESULT {ENGINE} FAIL reason={code}")
        return 1

    # --- ASSERT: a non-failed peak GeoClawDepthLayerURI + a sane metric --------
    if not isinstance(peak, GeoClawDepthLayerURI):
        print(f"\nFAIL: composer returned {type(peak).__name__}, not GeoClawDepthLayerURI")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_layer")
        return 1

    run_id = (peak.layer_id or "unknown").rsplit("-", 1)[-1]
    max_depth_m = float(peak.max_depth_m)
    print("\n=== result layer ===")
    print(f"  layer_id:     {peak.layer_id}")
    print(f"  role:         {peak.role}  style: {peak.style_preset}")
    print(f"  uri:          {peak.uri}")
    print(f"  max_depth_m:  {max_depth_m}")
    print(f"  flooded_area_km2: {peak.flooded_area_km2}")
    print(f"  scenario:     {peak.scenario}")

    if not (math.isfinite(max_depth_m) and max_depth_m >= 0.0):
        print("\nFAIL: max_depth_m is NaN/inf/negative - the solve produced no sane field.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=bad_metric")
        return 1

    print("\n=== RESULT ===")
    print(
        f"PASS: GeoClaw Batch solve ran, postprocess produced a peak depth layer, "
        f"max_depth_m={max_depth_m:.4g} m."
    )
    print(
        f"READINESS_RESULT {ENGINE} PASS run_id={run_id} layers=1 "
        f"metric=max_depth_m={max_depth_m:.6g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
