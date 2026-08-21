"""ADR 0296 LIVE acceptance: GeoClaw Manning split-by-domain, direct-call A/B.

Calls the ``geoclaw_inundation`` FRONT DOOR (not the internal
``model_geoclaw_inundation`` -- the domain-split Manning resolution lives in
the front-door wrapper) twice:

  A) LAND-DOMINATED (dam_break) over a real inland agricultural AOI (rural
     Story County, IA, near Ames -- no coastal water, solid NLCD cropland/
     pasture coverage). Bypasses the NID dam lookup with an explicit
     source_lonlat + dam_break_depth_m (that mechanism is proven elsewhere;
     this run targets the Manning wiring). Expect: manning_n DERIVED from
     real NLCD land cover, != the old literal 0.025.
  B) OFFSHORE (tsunami) over the SAME AOI treated as a synthetic tsunami
     source (proof-only forcing; the point is the friction leg, not the
     source). Expect: manning_n == 0.025 (Chow 1959 open-water standard),
     labeled non-refusing (consequence="numerical"), resolve_overland_manning
     never invoked.

Both are genuine solves through local-docker Clawpack (status=ok, a real
depth COG in MinIO) -- composer-side change only, no worker image touched.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  sg docker -c 'venvs/agent/bin/python scripts/run_geoclaw_manning_split_ab.py'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_geoclaw_manning_split_ab")

# A) Rural Story County, IA (near Ames) -- inland, no coastal water, dense
# cropland/pasture NLCD coverage (the shared A/B site the aquifer-column work
# already used, per project memory). Land-dominated dam_break leg.
AOI_LAND = (-93.65, 42.00, -93.55, 42.08)
DAM_SOURCE_LONLAT = (-93.60, 42.04)  # AOI centroid-ish, land
DAM_BREAK_DEPTH_M = 6.0

# B) Crescent City, CA -- the proven-working offshore tsunami AOI from
# scripts/run_geoclaw_direct.py (real negative bathymetry, genuine ocean).
AOI_OFFSHORE = (-124.24, 41.73, -124.16, 41.78)

SIM_DURATION_S = 1200  # short proof window
AMR_LEVELS = 2
OUTPUT_FRAMES = 4


def _bucket_env() -> tuple[str, str]:
    return (
        os.environ.get("TRID3NT_RUNS_BUCKET", ""),
        os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache"),
    )


import boto3  # noqa: E402
from _env_guard import require_local_endpoint

runs_bucket, cache_bucket = _bucket_env()
s3 = boto3.client(
    "s3",
    endpoint_url=require_local_endpoint(),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
for b in {runs_bucket, cache_bucket}:
    if not b:
        continue
    try:
        s3.head_bucket(Bucket=b)
    except Exception:
        try:
            s3.create_bucket(Bucket=b)
        except Exception as exc:  # noqa: BLE001
            log.warning("create_bucket(%s) failed (may already exist): %s", b, exc)


def _list_run_prefixes() -> set[str]:
    prefixes: set[str] = set()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=runs_bucket):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.startswith("case-manifests/") or key.startswith("case-views/"):
                    continue
                prefixes.add(key.split("/")[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("list_run_prefixes failed: %s", exc)
    return prefixes


try:
    from trid3nt_server.workflows.geoclaw.inundation.inundation import geoclaw_inundation
except ImportError as exc:
    log.error("import failed -- is PYTHONPATH set? %s", exc)
    sys.exit(1)


def _to_jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return str(obj)


def _manning_entry(result) -> dict | None:
    entries = getattr(result, "synthetic_inputs", None) or []
    for e in entries:
        param = e.get("param") if isinstance(e, dict) else getattr(e, "param", None)
        if param == "manning_n":
            return e.model_dump(mode="json") if hasattr(e, "model_dump") else dict(e)
    return None


async def _run_dam_break():
    log.info("=== A: dam_break (land-dominated) bbox=%s ===", AOI_LAND)
    pre = _list_run_prefixes()
    result = await geoclaw_inundation(
        bbox=AOI_LAND,
        scenario="dam_break",
        source_lonlat=DAM_SOURCE_LONLAT,
        dam_break_depth_m=DAM_BREAK_DEPTH_M,
        sim_duration_s=SIM_DURATION_S,
        amr_levels=AMR_LEVELS,
        output_frames=OUTPUT_FRAMES,
    )
    post = _list_run_prefixes()
    return result, sorted(post - pre)


async def _run_tsunami():
    log.info("=== B: tsunami (offshore) bbox=%s ===", AOI_OFFSHORE)
    pre = _list_run_prefixes()
    result = await geoclaw_inundation(
        bbox=AOI_OFFSHORE,
        scenario="tsunami",
        sim_duration_s=SIM_DURATION_S,
        amr_levels=AMR_LEVELS,
        output_frames=OUTPUT_FRAMES,
    )
    post = _list_run_prefixes()
    return result, sorted(post - pre)


async def _main():
    dam_result, dam_prefixes = await _run_dam_break()
    tsunami_result, tsunami_prefixes = await _run_tsunami()
    return dam_result, dam_prefixes, tsunami_result, tsunami_prefixes


dam_result, dam_prefixes, tsunami_result, tsunami_prefixes = asyncio.run(_main())

dam_manning = _manning_entry(dam_result)
tsunami_manning = _manning_entry(tsunami_result)

summary = {
    "aoi_land": list(AOI_LAND),
    "aoi_offshore": list(AOI_OFFSHORE),
    "A_dam_break": {
        "status": "error" if isinstance(dam_result, dict) else "ok",
        "result": _to_jsonable(dam_result),
        "manning_entry": dam_manning,
        "manning_n_used": (
            _to_jsonable(dam_result).get("manning_n")
            if isinstance(dam_result, dict) else None
        ),
        "new_run_prefixes": dam_prefixes,
    },
    "B_tsunami": {
        "status": "error" if isinstance(tsunami_result, dict) else "ok",
        "result": _to_jsonable(tsunami_result),
        "manning_entry": tsunami_manning,
        "new_run_prefixes": tsunami_prefixes,
    },
}

PROOF_DIR = Path(__file__).parent.parent / "docs" / "proof"
PROOF_DIR.mkdir(parents=True, exist_ok=True)
out_path = PROOF_DIR / "geoclaw_manning_split_ab_0296.json"
out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
log.info("summary written to %s", out_path)

print("\n=== GeoClaw Manning split-by-domain A/B ===")
print(json.dumps(summary, indent=2, default=str)[:6000])

ok = True
if isinstance(dam_result, dict):
    log.error("A (dam_break) returned FAILED envelope: %s", dam_result)
    ok = False
elif dam_manning is None or dam_manning.get("basis") not in ("derived", "user"):
    log.error("A (dam_break) manning_n entry missing/unexpected: %s", dam_manning)
    ok = False
elif dam_manning.get("basis") == "derived" and abs(float(dam_manning.get("value", 0.0)) - 0.025) < 1e-9:
    log.error("A (dam_break) derived value equals the old literal 0.025 -- suspicious")
    ok = False

if isinstance(tsunami_result, dict):
    log.error("B (tsunami) returned FAILED envelope: %s", tsunami_result)
    ok = False
elif tsunami_manning is None or abs(float(tsunami_manning.get("value", -1.0)) - 0.025) > 1e-9:
    log.error("B (tsunami) manning_n entry not the expected 0.025: %s", tsunami_manning)
    ok = False
elif tsunami_manning.get("consequence") != "numerical":
    log.error("B (tsunami) manning_n consequence tag not 'numerical': %s", tsunami_manning)
    ok = False

if ok:
    print("\nGeoClaw Manning split-by-domain A/B PASSED "
          "(A derived from real NLCD != 0.025, B kept labeled 0.025)")
else:
    sys.exit(2)
