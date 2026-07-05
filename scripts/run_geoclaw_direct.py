"""Direct GeoClaw tsunami inundation invocation for trid3nt-local proof.

Bypasses the LLM/agent chat layer -- calls the deterministic GeoClaw workflow
(model_dambreak_geoclaw_scenario) directly via the tool wrapper. The workflow runs:
  fetch_dem (USGS 3DEP / ETOPO topobathy) + reproject to EPSG:4326
  -> stage_geoclaw_manifest (build_spec + DEM reference to MinIO)
  -> run_solver('geoclaw', local-docker: trid3nt-local/geoclaw:latest)
     container receives --run-id <id> --manifest-uri s3://trid3nt-cache/...
     xgeoclaw compiles (first-run one-time cost) then runs the tsunami solve
  -> wait_for_completion (polls s3://trid3nt-runs/<run_id>/completion.json)
  -> postprocess_geoclaw (fort.q AMR frames -> peak inundation COG)
  -> publish_layer (TiTiler wave-height COG)

Genuine execution: a docker container named <run_id> runs the Clawpack
GeoClaw solver against the staged deck; outputs (fort.q frames + COGs) land
in MinIO under trid3nt-runs/<run_id>/.

Run:
  cd /home/nate/Documents/trid3nt-local
  sg docker -c 'env $(grep -v "^#" .env.local | xargs) \\
    PYTHONPATH=vendor/services/agent/src:vendor/packages/contracts/src \\
    venvs/agent/bin/python scripts/run_geoclaw_direct.py'
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
log = logging.getLogger("run_geoclaw_direct")

# Crescent City, CA -- ~5km coastal box suitable for a small tsunami scenario.
# lat 41.756 lon -124.20 from the task spec.
BBOX = (-124.24, 41.73, -124.16, 41.78)
SCENARIO = "tsunami"
SIM_DURATION_S = 1800  # 30 minutes -- short enough for a proof run
AMR_LEVELS = 2          # coarsest: level-1 base + level-2 AMR (fastest compile+solve)
OUTPUT_FRAMES = 6       # minimal frame count

# ---------------------------------------------------------------------------
# Sanity: local-docker backend + image + runs dir
# ---------------------------------------------------------------------------

backend = os.environ.get("GRACE2_SOLVER_BACKEND", "")
image = os.environ.get("GRACE2_GEOCLAW_IMAGE", "")
runs_bucket = os.environ.get("GRACE2_RUNS_BUCKET", "")
log.info(
    "backend=%s geoclaw_image=%s runs_bucket=%s endpoint=%s",
    backend, image, runs_bucket, os.environ.get("AWS_ENDPOINT_URL"),
)
if backend != "local-docker":
    log.warning("GRACE2_SOLVER_BACKEND is %r (expected local-docker)", backend)

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
for b in {runs_bucket, os.environ.get("GRACE2_CACHE_BUCKET", "trid3nt-cache")}:
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
# Invoke the GeoClaw workflow directly via the tool wrapper
# ---------------------------------------------------------------------------

try:
    from grace2_agent.workflows.model_dambreak_geoclaw_scenario import (
        model_dambreak_geoclaw_scenario,
    )
    from grace2_contracts.geoclaw_contracts import GeoClawRunArgs
except ImportError as exc:
    log.error("import failed -- is PYTHONPATH set? %s", exc)
    sys.exit(1)


async def _run():
    log.info(
        "invoking model_dambreak_geoclaw_scenario bbox=%s scenario=%s "
        "duration=%ds amr_levels=%d frames=%d",
        BBOX, SCENARIO, SIM_DURATION_S, AMR_LEVELS, OUTPUT_FRAMES,
    )
    run_args = GeoClawRunArgs(
        scenario=SCENARIO,
        bbox=BBOX,
        sim_duration_s=SIM_DURATION_S,
        amr_levels=AMR_LEVELS,
        output_frames=OUTPUT_FRAMES,
    )
    result = await model_dambreak_geoclaw_scenario(run_args)
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

# List objects under each new run prefix for evidence.
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
    "engine": "geoclaw",
    "backend": backend,
    "image": image,
    "bbox": list(BBOX),
    "scenario": SCENARIO,
    "sim_duration_s": SIM_DURATION_S,
    "amr_levels": AMR_LEVELS,
    "output_frames": OUTPUT_FRAMES,
    "result_type": type(result).__name__,
    "result": result_json,
    "new_run_prefixes": new_prefixes,
    "run_listings": run_listings,
    "tile_server_base": os.environ.get("GRACE2_TILE_SERVER_BASE"),
}

out_path = PROOF_DIR / "geoclaw_direct_result.json"
out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
log.info("summary written to %s", out_path)

print("\n=== GeoClaw direct run COMPLETE ===")
print(json.dumps(summary, indent=2, default=str)[:4000])

# Exit non-zero if the workflow returned a failed envelope.
if isinstance(result, dict) and result.get("error_code"):
    log.error("workflow returned FAILED envelope: %s", result.get("error_code"))
    sys.exit(2)
if not new_prefixes:
    log.error("no new MinIO run prefix -- solve did not produce outputs")
    sys.exit(3)
print("\nGeoClaw direct run PASSED (new run prefix + outputs in MinIO)")
