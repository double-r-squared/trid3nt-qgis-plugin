"""Direct Landlab landslide-susceptibility invocation for trid3nt-local proof.

Bypasses the LLM/agent chat layer -- calls the deterministic Landlab workflow
(``model_landslide_scenario``) directly. The workflow runs the FULL local chain:
  fetch_dem (USGS 3DEP 1m -> 10m fallback)
  -> stage_landlab_manifest (DEM + build_spec -> MinIO)
  -> run_solver('landlab') with GRACE2_SOLVER_BACKEND=local-docker
     (LocalSolverSpec: subprocess run_chain.py shim, exec_kind='exec')
  -> wait_for_completion (polls s3://<runs_bucket>/<run_id>/completion.json)
  -> postprocess_landlab -> publish_layer (TiTiler susceptibility COG)

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) \
    PYTHONPATH=server/src:contracts/src:. \
    venvs/agent/bin/python scripts/run_landlab_direct.py
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
log = logging.getLogger("run_landlab_direct")

# Small hilly area near Boulder, CO -- ~4km box
BBOX = (-105.37, 39.998, -105.33, 40.032)
ANALYSIS = "landslide_probability"
TARGET_RESOLUTION_M = 30.0  # coarsest default
N_MONTE_CARLO = 25  # small for speed

# ---------------------------------------------------------------------------
# Sanity: local-docker backend
# ---------------------------------------------------------------------------

backend = os.environ.get("GRACE2_SOLVER_BACKEND", "")
runs_bucket = os.environ.get("GRACE2_RUNS_BUCKET", "")
log.info(
    "backend=%s runs_bucket=%s endpoint=%s",
    backend, runs_bucket, os.environ.get("AWS_ENDPOINT_URL"),
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
# Invoke the Landlab landslide workflow directly
# ---------------------------------------------------------------------------

try:
    from grace2_agent.workflows.model_landslide_scenario import model_landslide_scenario
    from grace2_contracts.landlab_contracts import LandlabRunArgs
except ImportError as exc:
    log.error("import failed -- is PYTHONPATH set? %s", exc)
    sys.exit(1)


async def _run():
    log.info(
        "invoking model_landslide_scenario bbox=%s analysis=%s res=%.1fm n_mc=%d",
        BBOX, ANALYSIS, TARGET_RESOLUTION_M, N_MONTE_CARLO,
    )
    run_args = LandlabRunArgs(
        bbox=BBOX,
        analysis=ANALYSIS,
        target_resolution_m=TARGET_RESOLUTION_M,
        n_monte_carlo=N_MONTE_CARLO,
    )
    result = await model_landslide_scenario(run_args, compute_class="small")
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
    "engine": "landlab",
    "backend": "local-exec-subprocess",
    "bbox": list(BBOX),
    "analysis": ANALYSIS,
    "target_resolution_m": TARGET_RESOLUTION_M,
    "n_monte_carlo": N_MONTE_CARLO,
    "result_type": type(result).__name__,
    "result": result_json,
    "new_run_prefixes": new_prefixes,
    "run_listings": run_listings,
    "tile_server_base": os.environ.get("GRACE2_TILE_SERVER_BASE"),
}

out_path = PROOF_DIR / "landlab_direct_result.json"
out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
log.info("summary written to %s", out_path)

print("\n=== Landlab direct run COMPLETE ===")
print(json.dumps(summary, indent=2, default=str)[:4000])

if isinstance(result, dict) and result.get("error_code"):
    log.error("workflow returned FAILED envelope: %s", result.get("error_code"))
    sys.exit(2)
if not new_prefixes:
    log.error("no new MinIO run prefix -- solve did not produce outputs")
    sys.exit(3)
print("\nLandlab direct run PASSED (new run prefix + outputs in MinIO)")
