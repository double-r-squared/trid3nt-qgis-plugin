"""Direct SFINCS pluvial flood invocation for trid3nt-local proof.

Bypasses the LLM/agent chat layer -- calls the deterministic flood workflow
(``run_model_flood_scenario``) directly. The workflow runs the FULL chain:
  fetch_dem / fetch_landcover -> lookup_precip_return_period
  -> build_sfincs_model (hydromt-sfincs, in-agent)
  -> run_solver (GRACE2_SOLVER_BACKEND=local-docker: deltares/sfincs-cpu container)
  -> wait_for_completion (polls s3://<runs_bucket>/<run_id>/completion.json)
  -> postprocess_flood -> publish_layer (TiTiler depth COG)

Genuine execution: a docker container named <run_id> runs sfincs against the
staged deck mounted at /data; outputs land in MinIO under trid3nt-runs/<run_id>/.

Run:
  cd /home/nate/Documents/trid3nt-local
  sg docker -c 'env $(grep -v "^#" .env.local | xargs) \
    PYTHONPATH=vendor/services/agent/src:vendor/packages/contracts/src \
    venvs/agent/bin/python scripts/run_sfincs_direct.py'
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
log = logging.getLogger("run_sfincs_direct")

# Downtown Chattanooga, TN -- approx 4km box.
BBOX = (-85.32, 35.03, -85.28, 35.07)
RETURN_PERIOD_YR = 100
DURATION_HR = 1

# ---------------------------------------------------------------------------
# Sanity: local-docker backend + image + runs dir
# ---------------------------------------------------------------------------

backend = os.environ.get("GRACE2_SOLVER_BACKEND", "")
image = os.environ.get("GRACE2_SFINCS_IMAGE", "")
runs_dir = os.environ.get("GRACE2_RUNS_DIR", "")
runs_bucket = os.environ.get("GRACE2_RUNS_BUCKET", "")
log.info(
    "backend=%s image=%s runs_dir=%s runs_bucket=%s endpoint=%s",
    backend, image, runs_dir, runs_bucket, os.environ.get("AWS_ENDPOINT_URL"),
)
if backend != "local-docker":
    log.warning("GRACE2_SOLVER_BACKEND is %r (expected local-docker)", backend)

# ---------------------------------------------------------------------------
# Ensure MinIO buckets exist (cache + runs)
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
# Invoke the flood workflow directly
# ---------------------------------------------------------------------------

try:
    from grace2_agent.workflows.model_flood_scenario import run_model_flood_scenario
except ImportError as exc:
    log.error("import failed -- is PYTHONPATH set? %s", exc)
    sys.exit(1)


async def _run():
    log.info(
        "invoking run_model_flood_scenario bbox=%s pluvial rp=%dyr dur=%dhr class=small",
        BBOX, RETURN_PERIOD_YR, DURATION_HR,
    )
    result = await run_model_flood_scenario(
        bbox=BBOX,
        return_period_yr=RETURN_PERIOD_YR,
        duration_hr=DURATION_HR,
        compute_class="small",
        # pluvial defaults (no coastal / quadtree / surge)
    )
    return result


result = asyncio.run(_run())

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

log.info("workflow returned type=%s", type(result).__name__)

post_prefixes = list_run_prefixes()
new_prefixes = sorted(post_prefixes - pre_prefixes)
log.info("NEW MinIO run prefixes: %s", new_prefixes)

# Serialize the result (LayerURI pydantic model or a failed-envelope dict).
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
    "engine": "sfincs",
    "backend": backend,
    "image": image,
    "bbox": list(BBOX),
    "flood_type": "pluvial",
    "return_period_years": RETURN_PERIOD_YR,
    "duration_hours": DURATION_HR,
    "result_type": type(result).__name__,
    "result": result_json,
    "new_run_prefixes": new_prefixes,
    "run_listings": run_listings,
    "tile_server_base": os.environ.get("GRACE2_TILE_SERVER_BASE"),
}

out_path = PROOF_DIR / "sfincs_direct_result.json"
out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
log.info("summary written to %s", out_path)

print("\n=== SFINCS direct run COMPLETE ===")
print(json.dumps(summary, indent=2, default=str)[:4000])

# Exit non-zero if the workflow returned a failed envelope (dict with error_code).
if isinstance(result, dict) and result.get("error_code"):
    log.error("workflow returned FAILED envelope: %s", result.get("error_code"))
    sys.exit(2)
if not new_prefixes:
    log.error("no new MinIO run prefix -- solve did not produce outputs")
    sys.exit(3)
print("\nSFINCS direct run PASSED (new run prefix + outputs in MinIO)")
