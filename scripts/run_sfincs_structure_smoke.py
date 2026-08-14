"""SFINCS hydraulic-structure present-vs-absent DISCRIMINANT smoke (ADR 0256).

Runs the live registered ``sfincs_flood`` template twice on the SAME small AOI
under the SAME design storm, then compares the published flood-depth COGs:

  A) plain deck (no structure)
  B) a THIN DAM (thd) no-flow barrier line across the domain

A thin dam is a hard no-flow wall between cells: with rain-on-grid + terrain
routing the barrier must BLOCK/redirect water, so the depth field on the two
sides of the line diverges from the plain run. The discriminant is the max
absolute depth difference (plain vs thd) along/near the structure -- a structure
that did nothing would leave the field byte-identical.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
  env PYTHONPATH=server/src:contracts/src venvs/agent/bin/python \
    scripts/run_sfincs_structure_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_sfincs_structure_smoke")

# Small ~4 km AOI over Chattanooga, TN (same box as run_sfincs_direct.py).
BBOX = (-85.32, 35.03, -85.28, 35.07)
RETURN_PERIOD_YR = 100
DURATION_HR = 3

# A N-S thin-dam line down the middle of the AOI (lon ~ -85.30), spanning most of
# the domain height. Drawn/pushed line geometry -> one polyline of [lon,lat].
STRUCTURE_LINE = [[-85.300, 35.038], [-85.300, 35.062]]

import boto3

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
RUNS_BUCKET = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
for b in {RUNS_BUCKET, os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")}:
    try:
        s3.head_bucket(Bucket=b)
    except Exception:
        try:
            s3.create_bucket(Bucket=b)
        except Exception:
            pass

from trid3nt_server.agent.workflows.sfincs.flood.flood import sfincs_flood  # noqa: E402


def _uri_of(result) -> str | None:
    if hasattr(result, "uri"):
        return result.uri
    if isinstance(result, dict):
        # failed-envelope shape
        for lyr in (result.get("layers") or []):
            if isinstance(lyr, dict) and lyr.get("layer_type") == "raster":
                return lyr.get("uri")
    return None


async def _run():
    log.info("=== RUN A: plain flood (no structure) ===")
    plain = await sfincs_flood(
        bbox=BBOX, return_period_yr=RETURN_PERIOD_YR, duration_hr=DURATION_HR,
        compute_class="small",
    )
    log.info("plain result type=%s uri=%s", type(plain).__name__, _uri_of(plain))

    log.info("=== RUN B: flood + THIN DAM barrier ===")
    thd = await sfincs_flood(
        bbox=BBOX, return_period_yr=RETURN_PERIOD_YR, duration_hr=DURATION_HR,
        compute_class="small",
        structure_lines=[STRUCTURE_LINE], structure_type="thd",
    )
    log.info("thd result type=%s uri=%s", type(thd).__name__, _uri_of(thd))
    return plain, thd


plain, thd = asyncio.run(_run())
plain_uri, thd_uri = _uri_of(plain), _uri_of(thd)

# --- Compare the two depth COGs on a common grid ---
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.io import MemoryFile  # noqa: E402
from rasterio.warp import reproject, Resampling  # noqa: E402
from trid3nt_server.agent.tools.cache import read_object_bytes_s3  # noqa: E402


def _read_depth(uri: str):
    with MemoryFile(read_object_bytes_s3(uri)) as mf, mf.open() as ds:
        arr = ds.read(1, masked=True).filled(0.0).astype("float64")
        return arr, ds.transform, ds.crs, ds.width, ds.height


summary: dict = {
    "engine": "sfincs", "aoi": list(BBOX), "structure_type": "thd",
    "structure_line": STRUCTURE_LINE,
    "plain_uri": plain_uri, "thd_uri": thd_uri,
}

if plain_uri and thd_uri:
    a, ta, ca, wa, ha = _read_depth(plain_uri)
    b, tb, cb, wb, hb = _read_depth(thd_uri)
    # Resample thd onto the plain grid for a cell-aligned diff.
    b_on_a = np.zeros_like(a)
    reproject(source=b, destination=b_on_a, src_transform=tb, src_crs=cb,
              dst_transform=ta, dst_crs=ca, resampling=Resampling.bilinear)
    diff = b_on_a - a
    summary.update({
        "plain_wet_cells": int((a > 0.05).sum()),
        "thd_wet_cells": int((b_on_a > 0.05).sum()),
        "plain_max_depth_m": float(a.max()),
        "thd_max_depth_m": float(b_on_a.max()),
        "max_abs_depth_diff_m": float(np.abs(diff).max()),
        "mean_abs_depth_diff_m": float(np.abs(diff).mean()),
        "n_cells_diff_gt_5cm": int((np.abs(diff) > 0.05).sum()),
        "discriminated": bool(np.abs(diff).max() > 0.05),
    })

PROOF = Path(__file__).parent.parent / "docs" / "proof"
PROOF.mkdir(parents=True, exist_ok=True)
out = PROOF / "sfincs_structure_smoke_result.json"
out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print("\n=== SFINCS STRUCTURE SMOKE ===")
print(json.dumps(summary, indent=2, default=str))

if not (plain_uri and thd_uri):
    log.error("one or both runs did not publish a depth COG")
    sys.exit(2)
if not summary.get("discriminated"):
    log.error("structure produced NO depth difference -- not discriminating")
    sys.exit(3)
print("\nSFINCS structure present-vs-absent DISCRIMINATED")
