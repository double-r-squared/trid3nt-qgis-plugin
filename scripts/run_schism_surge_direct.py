"""Direct SCHISM PaHM storm-surge invocation for trid3nt-local proof (ADR 0217).

Bypasses the LLM/agent chat layer -- calls the REGISTERED template
(``schism_pahm_surge``) directly against MinIO + the local-docker schism image.
Default: the published Hurricane Ike (2008) best track over the NW Gulf / Galveston
AOI, synthetic sloping shelf bathymetry (a screening surge).

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v '^#' .env.local | xargs) PYTHONPATH=server/src:contracts/src \
    venvs/agent/bin/python scripts/run_schism_surge_direct.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_schism_surge_direct")

STORM = os.environ.get("SURGE_STORM") or None
YEAR = int(os.environ["SURGE_YEAR"]) if os.environ.get("SURGE_YEAR") else None
SIM_DAYS = float(os.environ.get("SURGE_SIM_DAYS", "1.5"))

import boto3

s3 = boto3.client(
    "s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
for b in {os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs"),
          os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")}:
    try:
        s3.head_bucket(Bucket=b)
    except Exception:
        try:
            s3.create_bucket(Bucket=b)
        except Exception:
            pass

from trid3nt_server.agent.tools import TOOL_REGISTRY  # noqa: E402


async def main() -> int:
    fn = TOOL_REGISTRY["schism_pahm_surge"].fn
    res = await fn(storm_name=STORM, year=YEAR, sim_days=SIM_DAYS, input_mode="auto")
    if isinstance(res, dict):
        log.error("SURGE FAILED: %s", json.dumps(res, indent=2)[:800])
        return 1
    print("=== SURGE RESULT ===")
    print("layer_id      :", res.layer_id)
    print("uri           :", res.uri)
    print("peak_surge_m  :", res.elev_max_m)
    print("trough_m      :", res.elev_min_m)
    print("surge_range_m :", res.tidal_range_m)
    print("n_nodes       :", res.n_nodes)
    print("mesh_source   :", res.mesh_source)
    print("bbox          :", res.bbox)
    print("fallback_note :", (res.fallback_note or "")[:400])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
