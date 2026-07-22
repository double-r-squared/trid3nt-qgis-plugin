"""LIVE readiness acceptance for the MODFLOW river-seepage engine.

Runs ON the agent box (the deployed grace2_agent editable install + the
aws-batch solver env). NO LLM, NO agent loop: it calls the REAL
``model_river_seepage_scenario`` workflow function DIRECTLY, which fetches the
river flowline for the AOI, drapes a RIV head-dependent boundary onto a MODFLOW 6
grid, runs the GWF + MF6-GWT solver (``solver='modflow'`` on AWS Batch), and
returns a ``RiverSeepageResult`` carrying the typed gaining/losing seepage layer.

Modeled on ``verify_mexico_beach_waves.py``: env sanity print, import the real
workflow, build minimal-but-valid inputs, call with ``await``, assert a non-
failed result with the seepage layer + a sane metric, print a clear PASS/FAIL
line, return exit code 0/1.

Minimal inputs (smallest valid river-seepage run):
  * spill_location_latlon: (26.64, -81.87) - the Caloosahatchee reach near Fort
    Myers, FL the engine test (test_river_seepage.py) uses, so a real river
    flowline is found and the RIV boundary lands on the grid.
  * contaminant: "TCE", release_rate_kg_s: 0.01, duration_days: 30 (the contract
    defaults; the demo-domain MF6 grid is tiny + fixed by DEFAULT_AOI_HALF_DEG so
    these do not blow the cell budget).
  * fetch_dem_for_streambed: False (the v0.1 RIV demo runs on demo streambed
    defaults; skipping the DEM keeps the chain lean - the test mocks it absent).
  * along_river_source: True (the seepage source - the default).

PASS gate: the returned ``RiverSeepageResult.seepage_layer`` (a SeepageLayerURI)
carries a renderable uri + ``river_cell_count`` >= 1 + a finite leakage scalar.

USAGE (run ON the agent box i-0251879a278df797f):
    cd services/agent && python verify_readiness_river_seepage.py

Required env (set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_AWS_BATCH_JOB_DEF_MODFLOW=<modflow job-def>   (or generic _JOB_DEF)
    GRACE2_AWS_BATCH_QUEUE=grace2-solvers
    GRACE2_RUNS_BUCKET=<runs bucket>
    GRACE2_CACHE_BUCKET=<cache bucket>
    AWS_REGION / AWS_DEFAULT_REGION=us-west-2
    (GRACE2_MODFLOW_LOCAL MUST be unset/falsey so the Batch lane is used.)
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

ENGINE = "river_seepage"

# A real river reach (Caloosahatchee, Fort Myers FL) the engine test uses, so
# fetch_river_geometry finds a flowline and the RIV boundary draped onto the
# (tiny demo) MF6 grid actually exchanges water with the aquifer.
SPILL_LATLON: tuple[float, float] = (26.64, -81.87)


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="contract-default minimal run (default; the demo MF6 grid is fixed)",
    )
    args = ap.parse_args(argv)
    _ = args.tiny

    spill = SPILL_LATLON
    contaminant = "TCE"
    release_rate_kg_s = 0.01
    duration_days = 30.0

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_MODFLOW")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()
    modflow_local = (os.environ.get("GRACE2_MODFLOW_LOCAL") or "").strip().lower()

    print("=== MODFLOW river-seepage readiness acceptance (live AWS) ===")
    print(f"  engine:          {ENGINE}")
    print(f"  spill_latlon:    {spill}")
    print(f"  contaminant:     {contaminant}  rate_kg_s: {release_rate_kg_s}")
    print(f"  duration_days:   {duration_days}")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_MODFLOW(or generic)={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  GRACE2_MODFLOW_LOCAL={modflow_local!r} (must be falsey for Batch)")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' to dispatch the "
            "river-seepage MODFLOW solve to Batch."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=backend_not_aws_batch")
        return 2
    if modflow_local in {"1", "true", "yes", "on"}:
        print(
            "FAIL: GRACE2_MODFLOW_LOCAL is truthy - the river-seepage tool would "
            "run in-process, not on Batch. Unset it for the readiness sweep."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=modflow_local_set")
        return 2
    if not job_def:
        print(
            "FAIL: no MODFLOW job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_MODFLOW (or the generic "
            "GRACE2_AWS_BATCH_JOB_DEF)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_job_def")
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=missing_queue_or_runs_bucket")
        return 2

    # Import the REAL workflow (no LLM, no agent).
    from grace2_contracts.modflow_contracts import SeepageLayerURI
    from grace2_agent.workflows.model_river_seepage_scenario import (
        model_river_seepage_scenario,
    )

    print("\n--- running river-seepage chain (fetch river -> MF6 RIV+GWT solve) ---")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    try:
        result = await model_river_seepage_scenario(
            spill_location_latlon=spill,
            contaminant=contaminant,
            release_rate_kg_s=release_rate_kg_s,
            duration_days=duration_days,
            fetch_dem_for_streambed=False,
            along_river_source=True,
            compute_class="standard",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", type(exc).__name__)
        print(f"\nFAIL: composer raised {type(exc).__name__}: {exc}")
        print(f"READINESS_RESULT {ENGINE} FAIL reason={code}")
        return 1

    seepage = getattr(result, "seepage_layer", None)
    if not isinstance(seepage, SeepageLayerURI):
        print(f"\nFAIL: result.seepage_layer is {type(seepage).__name__}, not SeepageLayerURI")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_layer")
        return 1

    run_id = (seepage.layer_id or "unknown").rsplit("-", 1)[-1]
    total_leakage = float(seepage.total_leakage_m3_day)
    river_cells = int(seepage.river_cell_count)
    print("\n=== seepage layer ===")
    print(f"  layer_id:        {seepage.layer_id}")
    print(f"  role:            {seepage.role}  style: {seepage.style_preset}")
    print(f"  uri:             {seepage.uri}")
    print(f"  total_leakage_m3_day: {total_leakage}")
    print(f"  gaining_m3_day:  {seepage.gaining_m3_day}")
    print(f"  losing_m3_day:   {seepage.losing_m3_day}")
    print(f"  river_cell_count: {river_cells}")

    if river_cells < 1:
        print("\nFAIL: river_cell_count < 1 - no RIV reach landed on the grid.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_river_cells")
        return 1
    if not math.isfinite(total_leakage):
        print("\nFAIL: total_leakage_m3_day is NaN/inf - no sane seepage budget.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=bad_metric")
        return 1

    print("\n=== RESULT ===")
    print(
        f"PASS: river-seepage MF6 solve ran, postprocess produced a seepage layer "
        f"over {river_cells} RIV cells, total_leakage_m3_day={total_leakage:.4g}."
    )
    print(
        f"READINESS_RESULT {ENGINE} PASS run_id={run_id} layers=1 "
        f"metric=river_cell_count={river_cells}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
