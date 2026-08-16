"""Direct Landlab groundwater invocations for trid3nt-local proof (ADR 0214).

Bypasses the LLM/agent chat layer -- calls the deterministic Landlab groundwater
composers directly. Each runs the FULL local off-box chain:
  fetch_dem (USGS 3DEP 1m -> 10m fallback)
  -> stage_landlab_manifest (DEM + build_spec -> MinIO)
  -> run_solver('landlab') with TRID3NT_SOLVER_BACKEND=local-docker
     (LocalSolverSpec: subprocess run_chain.py shim, exec_kind='exec')
  -> wait_for_completion -> postprocess -> publish_layer

Site: Panola Mountain Research Watershed, GA (a classic USGS Piedmont
groundwater/baseflow/hillslope-hydrology research catchment).

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) \
    PYTHONPATH=.:contracts:. \
    venvs/agent/bin/python scripts/run_landlab_groundwater_direct.py [steady|storm|both]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("run_landlab_groundwater_direct")

# Panola Mountain Research Watershed, GA -- ~4 km box (humid Piedmont catchment).
BBOX = (-84.18, 33.60, -84.14, 33.64)
TARGET_RESOLUTION_M = 30.0

which = sys.argv[1] if len(sys.argv) > 1 else "both"

import boto3

runs_bucket = os.environ.get("TRID3NT_RUNS_BUCKET", "")
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
for b in {runs_bucket, os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")}:
    if not b:
        continue
    try:
        s3.head_bucket(Bucket=b)
    except Exception:
        try:
            s3.create_bucket(Bucket=b)
        except Exception as exc:  # noqa: BLE001
            log.warning("create_bucket(%s): %s", b, exc)

from trid3nt_contracts.landlab_contracts import LandlabRunArgs  # noqa: E402
from trid3nt_server.workflows.landlab.groundwater_storm_recession.groundwater_storm_recession import (  # noqa: E402
    model_landlab_groundwater_storm_recession,
)
from trid3nt_server.workflows.landlab.groundwater_water_table.groundwater_water_table import (  # noqa: E402
    model_landlab_groundwater_water_table,
)

PROOF_DIR = Path(__file__).parent.parent / "docs" / "proof"
PROOF_DIR.mkdir(parents=True, exist_ok=True)


async def run_steady():
    ra = LandlabRunArgs(
        bbox=BBOX,
        analysis="groundwater_steady",
        target_resolution_m=TARGET_RESOLUTION_M,
        gw_recharge_mm_yr=250.0,
        gw_aquifer_thickness_m=15.0,
    )
    r = await model_landlab_groundwater_water_table(ra, compute_class="small")
    return r


async def run_storm():
    ra = LandlabRunArgs(
        bbox=BBOX,
        analysis="groundwater_storm",
        target_resolution_m=TARGET_RESOLUTION_M,
        gw_storm_aquifer_thickness_m=6.0,
        gw_storm_mean_depth_mm=22.0,
        gw_storm_total_days=120.0,
    )
    r = await model_landlab_groundwater_storm_recession(ra, compute_class="small")
    return r


def _dump(tag, result):
    j = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    out = PROOF_DIR / f"landlab_groundwater_{tag}_result.json"
    out.write_text(json.dumps(j, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s", out)
    print(f"\n=== {tag} RESULT ===")
    print(json.dumps(j, indent=2, default=str)[:2500])
    if isinstance(result, dict) and result.get("error_code"):
        log.error("%s FAILED: %s", tag, result.get("error_code"))
        return False
    return True


ok = True
if which in ("steady", "both"):
    ok = _dump("steady", asyncio.run(run_steady())) and ok
if which in ("storm", "both"):
    ok = _dump("storm", asyncio.run(run_storm())) and ok

sys.exit(0 if ok else 2)
