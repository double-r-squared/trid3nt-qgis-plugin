#!/usr/bin/env python
"""P2 proof: drive a TELEMAC river-dye solve THROUGH the real registry seam.

NOT the agent, NOT an LLM tool, NOT a direct P1 call. This writes a
worker-contract manifest for the Snake River reach to MinIO, then invokes the
solve through ``tools.solver.run_solver(solver='telemac_river_dye', ...)`` under
GRACE2_SOLVER_BACKEND=local-docker -- i.e. the SAME dispatch path the agent uses
(LOCAL_SOLVER_SPEC_REGISTRY -> telemac_local_spec -> docker run
trid3nt-local/telemac:latest -v <rundir>:/data). It then polls
``wait_for_completion`` and confirms:
  * docker run completed,
  * completion.json status == ok,
  * a result r2d_river.slf landed in s3://trid3nt-runs/<run_id>/.

Env (MinIO), export before running:
  AWS_ENDPOINT_URL=http://100.92.163.46:9000
  AWS_ACCESS_KEY_ID=trid3nt  AWS_SECRET_ACCESS_KEY=trid3nt-local-dev
  GRACE2_CACHE_BUCKET=trid3nt-cache  GRACE2_RUNS_BUCKET=trid3nt-runs
  GRACE2_SOLVER_BACKEND=local-docker
  GRACE2_TELEMAC_IMAGE=trid3nt-local/telemac:latest
  GRACE2_RUNS_DIR=/home/nate/Documents/trid3nt-local/data/runs
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import boto3

# Register the telemac spec (imports run_telemac -> SOLVER/LOCAL registries).
from grace2_agent.workflows import run_telemac as _rt  # noqa: F401
from grace2_agent.tools.solver import run_solver, wait_for_completion


def _s3():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def main() -> int:
    cache_bucket = os.environ["GRACE2_CACHE_BUCKET"]
    runs_bucket = os.environ["GRACE2_RUNS_BUCKET"]
    run_tag = f"telemac-proof-{int(time.time())}"

    # A SMALL Snake River reach (shorter than the P1 default 6 km so the proof
    # solve is quick; same seed point, same physics path).
    manifest = {
        "reach": {
            "name": "snake_river_twin_falls_proof",
            "seed_lon": -114.307,
            "seed_lat": 42.579,
            "nav_direction": "DM",
            "distance_km": 3.0,
            "channel_width_m": 60.0,
            "mesh_size_m": 16.0,
            "inflow_q_m3s": 250.0,
            "init_depth_m": 2.5,
            "dye_conc_mgl": 100.0,
            "duration_s": 1200.0,
            "time_step_s": 1.0,
            "graphic_period": 200,
        },
        "run_id": run_tag,
        "inputs": [],           # the pipeline self-fetches NHDPlus + the DEM
        "telemac_args": [],     # the image CMD drives the entrypoint
        "outputs": [
            "r2d_river.slf", "river.slf", "river.cli",
            "t2d_river.cas", "full_listing.log", "telemac_metrics.json",
        ],
    }

    s3 = _s3()
    manifest_key = f"telemac/{run_tag}/manifest.json"
    s3.put_object(
        Bucket=cache_bucket, Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    manifest_uri = f"s3://{cache_bucket}/{manifest_key}"
    print(f"== wrote manifest -> {manifest_uri}")

    print("== run_solver(solver='telemac_river_dye') THROUGH the seam ==")
    handle = run_solver(
        solver="telemac_river_dye",
        model_setup_uri=manifest_uri,
        compute_class="medium",
    )
    print(f"   handle: run_id={handle.run_id} workflow_name={handle.workflow_name}")

    t0 = time.time()
    result = asyncio.run(
        wait_for_completion(handle, poll_interval_s=5, timeout_s=1800)
    )
    print(f"== wait_for_completion -> status={result.status} "
          f"error_code={getattr(result, 'error_code', None)} "
          f"({time.time() - t0:.0f}s)")

    # Read the completion.json the supervisor wrote to the runs bucket.
    comp_key = f"{handle.run_id}/completion.json"
    comp = json.loads(
        s3.get_object(Bucket=runs_bucket, Key=comp_key)["Body"].read().decode()
    )
    print("== completion.json ==")
    print(json.dumps(comp, indent=2))

    # Confirm the result .slf landed in MinIO.
    listing = s3.list_objects_v2(Bucket=runs_bucket, Prefix=f"{handle.run_id}/")
    objs = {o["Key"]: o["Size"] for o in listing.get("Contents", [])}
    print("== runs-bucket objects ==")
    for k, sz in sorted(objs.items()):
        print(f"   {k}  {sz} bytes")

    slf_key = f"{handle.run_id}/r2d_river.slf"
    ok = comp.get("status") == "ok" and slf_key in objs and objs[slf_key] > 0
    if ok:
        print(f"\nPROOF OK: status=ok, result .slf at s3://{runs_bucket}/{slf_key} "
              f"({objs[slf_key]} bytes)")
        return 0
    print(f"\nPROOF FAILED: status={comp.get('status')} "
          f"slf_present={slf_key in objs}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
