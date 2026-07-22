"""LIVE readiness acceptance for the OpenQuake (PSHA) seismic-hazard engine.

Runs ON the agent box (the deployed grace2_agent editable install + the
aws-batch solver env). NO LLM, NO agent loop: it calls the REAL
``model_seismic_hazard_scenario`` workflow function DIRECTLY, which stages a
build_spec, submits the ``openquake`` AWS Batch job, waits, downloads the
exported hazard-map CSV, and postprocesses it into a ``SeismicHazardLayerURI``.

Modeled on ``verify_mexico_beach_waves.py``: env sanity print, import the real
workflow, build minimal-but-valid inputs, call with ``await``, assert a non-
failed hazard layer + a sane metric, print a clear PASS/FAIL line, return exit
code 0/1.

Minimal inputs (smallest valid PSHA run):
  * bbox: a small ~0.2 x 0.2 deg AOI in the seismically-active Bay Area (the
    region pinned in test_openquake_engine.py: (-122.5, 37.5, -121.5, 38.5)),
    shrunk so the site grid is tiny.
  * site_grid_spacing_km: 20 km - COARSE on purpose so the small AOI yields very
    few PSHA sites (the cell budget driver; OpenQuake is RAM-hungry ~2 GB/thread).
  * max_distance_km: 100 (smaller integration distance = fewer ruptures).
  * imt/poe/investigation_time/gmpe: the contract defaults (PGA, 10% in 50 yr,
    BooreAtkinson2008) - the canonical 475-yr engineering hazard map.

PASS gate: the returned ``SeismicHazardLayerURI`` carries a renderable uri +
``n_sites`` >= 1 + a finite ``max_hazard_value`` >= 0.0.

USAGE (run ON the agent box i-0251879a278df797f):
    cd services/agent && python verify_readiness_openquake.py

Required env (set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_AWS_BATCH_JOB_DEF_OPENQUAKE=<openquake job-def>  (or generic _JOB_DEF)
    GRACE2_AWS_BATCH_QUEUE=grace2-solvers
    GRACE2_RUNS_BUCKET=<runs bucket>
    GRACE2_CACHE_BUCKET=<cache bucket>          (build_spec upload target)
    AWS_REGION / AWS_DEFAULT_REGION=us-west-2
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

ENGINE = "openquake"

# Small Bay-Area AOI (inside the test region (-122.5, 37.5, -121.5, 38.5)),
# shrunk so a COARSE 20 km site grid yields a tiny number of PSHA sites.
SMALL_BBOX: tuple[float, float, float, float] = (-122.30, 37.70, -122.10, 37.90)


def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tiny",
        action="store_true",
        help="small AOI + coarse 20km site grid (default; cheapest Batch run)",
    )
    args = ap.parse_args(argv)
    _ = args.tiny

    bbox = SMALL_BBOX
    site_grid_spacing_km = 20.0
    max_distance_km = 100.0

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_OPENQUAKE")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    cache_bucket = (os.environ.get("GRACE2_CACHE_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()

    print("=== OpenQuake PSHA readiness acceptance (live AWS) ===")
    print(f"  engine:          {ENGINE}")
    print(f"  bbox (EPSG4326): {_bbox_str(bbox)}")
    print(f"  imt:             PGA (default)  poe: 0.10  inv_time: 50yr")
    print(f"  site_grid_km:    {site_grid_spacing_km}  max_distance_km: {max_distance_km}")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_OPENQUAKE(or generic)={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  GRACE2_CACHE_BUCKET={cache_bucket!r} (build_spec upload target)")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' (OpenQuake is "
            "cloud-only; the engine is RAM-hungry + ships as a containerized CLI)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=backend_not_aws_batch")
        return 2
    if not job_def:
        print(
            "FAIL: no OpenQuake job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_OPENQUAKE (or the generic "
            "GRACE2_AWS_BATCH_JOB_DEF)."
        )
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_job_def")
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=missing_queue_or_runs_bucket")
        return 2

    # Import the REAL workflow + the run-args contract (no LLM, no agent).
    from grace2_contracts.openquake_contracts import (
        OpenQuakeRunArgs,
        SeismicHazardLayerURI,
    )
    from grace2_agent.workflows.model_seismic_hazard_scenario import (
        model_seismic_hazard_scenario,
    )

    run_args = OpenQuakeRunArgs(
        bbox=bbox,
        site_grid_spacing_km=site_grid_spacing_km,
        max_distance_km=max_distance_km,
    )

    print("\n--- submitting openquake Batch job (stage build_spec -> solve -> CSV) ---")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    try:
        layer = await model_seismic_hazard_scenario(
            run_args, compute_class="standard"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", type(exc).__name__)
        print(f"\nFAIL: composer raised {type(exc).__name__}: {exc}")
        print(f"READINESS_RESULT {ENGINE} FAIL reason={code}")
        return 1

    if not isinstance(layer, SeismicHazardLayerURI):
        print(f"\nFAIL: composer returned {type(layer).__name__}, not SeismicHazardLayerURI")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_layer")
        return 1

    run_id = (layer.layer_id or "unknown").rsplit("-", 1)[-1]
    max_hazard = float(layer.max_hazard_value)
    n_sites = int(layer.n_sites)
    print("\n=== hazard layer ===")
    print(f"  layer_id:        {layer.layer_id}")
    print(f"  role:            {layer.role}  style: {layer.style_preset}")
    print(f"  uri:             {layer.uri}")
    print(f"  imt:             {layer.imt}  return_period_yr: {layer.return_period_years}")
    print(f"  max_hazard_value: {max_hazard}  units: {layer.units}")
    print(f"  hazard_area_km2: {layer.hazard_area_km2}")
    print(f"  n_sites:         {n_sites}")

    if n_sites < 1:
        print("\nFAIL: n_sites < 1 - the PSHA produced no hazard sites.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=no_sites")
        return 1
    if not (math.isfinite(max_hazard) and max_hazard >= 0.0):
        print("\nFAIL: max_hazard_value is NaN/inf/negative - no sane hazard field.")
        print(f"READINESS_RESULT {ENGINE} FAIL reason=bad_metric")
        return 1

    print("\n=== RESULT ===")
    print(
        f"PASS: OpenQuake Batch solve ran, postprocess produced a hazard layer "
        f"over {n_sites} sites, max_hazard_value={max_hazard:.4g} {layer.units}."
    )
    print(
        f"READINESS_RESULT {ENGINE} PASS run_id={run_id} layers=1 "
        f"metric=max_hazard_value={max_hazard:.6g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
