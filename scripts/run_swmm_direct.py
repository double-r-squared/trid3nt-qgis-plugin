"""Direct PySWMM quasi-2D urban-flood invocation for trid3nt-local proof.

Bypasses the LLM/agent chat layer -- calls the deterministic urban-flood workflow
(``model_urban_flood_swmm``) directly. The workflow runs the FULL in-process chain:
  fetch_dem / fetch_buildings -> lookup_precip_return_period
  -> build_swmm_mesh (quasi-2D node/link deck)
  -> run_swmm_local (pyswmm IN-PROCESS, no container)
  -> postprocess_swmm -> publish_layer (TiTiler depth COG)

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) \
    PYTHONPATH=server/src:contracts/src:. \
    venvs/agent/bin/python scripts/run_swmm_direct.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("run_swmm_direct")

# Downtown Alexandria, VA -- approx 3 blocks (~0.003 deg box)
# The composer floors to a 300m square if too small.
BBOX = (-77.052, 38.802, -77.044, 38.808)
RETURN_PERIOD_YR = 10
STORM_DURATION_HR = 1.0
TARGET_RESOLUTION_M = 20.0

# ---------------------------------------------------------------------------
# Sanity: local-docker backend
# ---------------------------------------------------------------------------

backend = os.environ.get("TRID3NT_SOLVER_BACKEND", "")
runs_bucket = os.environ.get("TRID3NT_RUNS_BUCKET", "")
log.info(
    "backend=%s runs_bucket=%s endpoint=%s",
    backend, runs_bucket, os.environ.get("AWS_ENDPOINT_URL"),
)
# SWMM uses in-process pyswmm regardless of TRID3NT_SOLVER_BACKEND
# (TRID3NT_SWMM_LOCAL unset = local mode by default)

# ---------------------------------------------------------------------------
# Ensure MinIO buckets exist
# ---------------------------------------------------------------------------

import boto3

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
        log.info("bucket %s exists", b)
    except Exception:
        log.info("creating bucket %s ...", b)
        try:
            s3.create_bucket(Bucket=b)
        except Exception as exc:
            log.warning("create_bucket(%s) failed (may already exist): %s", b, exc)

# ---------------------------------------------------------------------------
# Snapshot MinIO runs prefixes BEFORE the run
# ---------------------------------------------------------------------------

def list_run_prefixes() -> set[str]:
    prefixes: set[str] = set()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=runs_bucket):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.startswith("case-manifests/") or key.startswith("case-views/"):
                    continue
                prefixes.add(key.split("/")[0])
    except Exception as exc:
        log.warning("list_run_prefixes failed: %s", exc)
    return prefixes


pre_prefixes = list_run_prefixes()
log.info("pre-run MinIO run prefixes: %s", sorted(pre_prefixes))

# ---------------------------------------------------------------------------
# Invoke the SWMM urban-flood workflow directly
# ---------------------------------------------------------------------------

try:
    from trid3nt_server.workflows.model_urban_flood_swmm import model_urban_flood_swmm
    from trid3nt_contracts.swmm_contracts import SWMMRunArgs
except ImportError as exc:
    log.error("import failed -- is PYTHONPATH set? %s", exc)
    sys.exit(1)


async def _run():
    log.info(
        "invoking model_urban_flood_swmm bbox=%s rp=%dyr dur=%.1fhr res=%.1fm",
        BBOX, RETURN_PERIOD_YR, STORM_DURATION_HR, TARGET_RESOLUTION_M,
    )
    run_args = SWMMRunArgs(
        bbox=BBOX,
        return_period_yr=RETURN_PERIOD_YR,
        storm_duration_hr=STORM_DURATION_HR,
        target_resolution_m=TARGET_RESOLUTION_M,
        building_representation="drop",
    )
    result = await model_urban_flood_swmm(run_args)
    return result


result = asyncio.run(_run())

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

log.info("workflow returned type=%s", type(result).__name__)

post_prefixes = list_run_prefixes()
new_prefixes = sorted(post_prefixes - pre_prefixes)
log.info("NEW MinIO run prefixes: %s", new_prefixes)


def _to_jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return str(obj)


result_json = _to_jsonable(result)

PROOF_DIR = Path(__file__).parent.parent / "docs" / "proof"
PROOF_DIR.mkdir(parents=True, exist_ok=True)

run_listings: dict[str, list[str]] = {}
for prefix in new_prefixes:
    keys = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=runs_bucket, Prefix=prefix + "/"):
            for obj in page.get("Contents", []) or []:
                keys.append(f"{obj['Key']} ({obj['Size']} bytes)")
    except Exception as exc:
        log.warning("listing %s failed: %s", prefix, exc)
    run_listings[prefix] = keys

summary = {
    "engine": "swmm",
    "backend": "local-pyswmm",
    "bbox": list(BBOX),
    "return_period_yr": RETURN_PERIOD_YR,
    "storm_duration_hr": STORM_DURATION_HR,
    "target_resolution_m": TARGET_RESOLUTION_M,
    "result_type": type(result).__name__,
    "result": result_json,
    "new_run_prefixes": new_prefixes,
    "run_listings": run_listings,
    "tile_server_base": os.environ.get("TRID3NT_TILE_SERVER_BASE"),
}

out_path = PROOF_DIR / "swmm_direct_result.json"
out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
log.info("summary written to %s", out_path)

print("\n=== SWMM direct run COMPLETE ===")
print(json.dumps(summary, indent=2, default=str)[:4000])

if isinstance(result, dict) and result.get("error_code"):
    log.error("workflow returned FAILED envelope: %s", result.get("error_code"))
    sys.exit(2)
if not new_prefixes:
    log.error("no new MinIO run prefix -- solve did not produce outputs")
    sys.exit(3)
print("\nSWMM direct run PASSED (new run prefix + outputs in MinIO)")
