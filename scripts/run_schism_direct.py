"""Direct SCHISM tidal-hydro invocation for trid3nt-local proof (engine #12, ADR 0118).

Bypasses the LLM/agent chat layer -- calls the REGISTERED template
(``schism_tidal_hydro``) directly. The chain runs:
  stage the deck (QuarterAnnulus fixture OR an authored coastal_tin deck)
  -> _stage_manifest (upload the case files as manifest inputs[] to MinIO)
  -> run_solver('schism_tidal_hydro', local-docker: trid3nt-local/schism:latest)
     the container bind-mounts the rundir at /data, runs pschism_TVD-VL under
     mpirun, gates on the "Run completed successfully" sentinel
  -> wait_for_completion (polls s3://trid3nt-runs/<run_id>/completion.json)
  -> postprocess_schism (out2d_1.nc -> max-elevation COG + UGRID mesh row)
  -> (QuarterAnnulus) verify_against_analytical vs the bundled M2 solution

Run (mesh_source via env SCHISM_MESH_SOURCE, default bundled_quarterannulus):
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) PYTHONPATH=.:contracts \\
    venvs/agent/bin/python scripts/run_schism_direct.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_schism_direct")

MESH_SOURCE = os.environ.get("SCHISM_MESH_SOURCE", "bundled_quarterannulus")
LOCATION = os.environ.get("SCHISM_LOCATION", "Galveston Bay")
CONSTITUENTS = (os.environ.get("SCHISM_CONSTITUENTS", "M2")).split(",")
AMP = float(os.environ.get("SCHISM_AMP", "0.4"))
SIM_DAYS = float(os.environ.get("SCHISM_SIM_DAYS", "5"))

backend = os.environ.get("TRID3NT_SOLVER_BACKEND", "")
image = os.environ.get("TRID3NT_SCHISM_IMAGE", "trid3nt-local/schism:latest")
runs_bucket = os.environ.get("TRID3NT_RUNS_BUCKET", "")
log.info("backend=%s schism_image=%s runs_bucket=%s endpoint=%s mesh_source=%s",
         backend, image, runs_bucket, os.environ.get("AWS_ENDPOINT_URL"), MESH_SOURCE)

import boto3

s3 = boto3.client(
    "s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
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
            log.info("created bucket %s", b)
        except Exception as exc:
            log.warning("create_bucket(%s): %s", b, exc)


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


pre = list_run_prefixes()

try:
    from trid3nt_server.workflows.schism.tidal_hydro.tidal_hydro import schism_tidal_hydro
except ImportError as exc:
    log.error("import failed -- PYTHONPATH? %s", exc)
    sys.exit(1)


async def _run():
    kwargs = dict(mesh_source=MESH_SOURCE, sim_days=SIM_DAYS)
    if MESH_SOURCE == "coastal_tin":
        kwargs.update(constituents=CONSTITUENTS, tidal_amplitude_m=AMP)
        bbox_env = os.environ.get("SCHISM_BBOX")
        if bbox_env:
            kwargs["bbox"] = [float(v) for v in bbox_env.split(",")]
        else:
            kwargs["location_query"] = LOCATION
    return await schism_tidal_hydro(**kwargs)


result = asyncio.run(_run())
log.info("workflow returned type=%s", type(result).__name__)

new_prefixes = sorted(list_run_prefixes() - pre)
log.info("NEW MinIO run prefixes: %s", new_prefixes)


def _to_jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj if isinstance(obj, dict) else str(obj)


summary = {
    "engine": "schism", "backend": backend, "image": image,
    "mesh_source": MESH_SOURCE, "result_type": type(result).__name__,
    "result": _to_jsonable(result), "new_run_prefixes": new_prefixes,
}
PROOF = Path(__file__).parent.parent / "docs" / "proof"
PROOF.mkdir(parents=True, exist_ok=True)
(PROOF / f"schism_direct_{MESH_SOURCE}.json").write_text(
    json.dumps(summary, indent=2, default=str), encoding="utf-8")

print("\n=== SCHISM direct run COMPLETE ===")
print(json.dumps(summary, indent=2, default=str)[:4000])

if isinstance(result, dict) and result.get("error_code"):
    log.error("workflow returned FAILED envelope: %s", result.get("error_code"))
    sys.exit(2)
if not new_prefixes:
    log.error("no new MinIO run prefix -- solve did not produce outputs")
    sys.exit(3)
print("\nSCHISM direct run PASSED (new run prefix + outputs in MinIO)")
