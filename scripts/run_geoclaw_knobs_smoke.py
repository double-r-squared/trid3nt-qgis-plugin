"""Live smoke for the GeoClaw CAND-S knob templates (AMR regions + regional Manning).

Calls the two new template tools directly against the local-docker GeoClaw solver
and reports, per run: the new MinIO run prefix, the depth-layer scalars, and the
``Total mass at initial time`` diagnostic parsed from the uploaded geoclaw.stdout
(~1e5 == no wave, ~1e9+ == a real wave). Cheap: coarse grid, short sim window.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  TRID3NT_GEOCLAW_IMAGE=trid3nt-local/geoclaw:knobs-test \
    PYTHONPATH=server/src:contracts/src \
    venvs/agent/bin/python scripts/run_geoclaw_knobs_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("geoclaw_knobs_smoke")

import boto3

runs_bucket = os.environ["TRID3NT_RUNS_BUCKET"]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)

# Crescent City, CA -- the reference proof AOI (real US coastal bathymetry).
BBOX = (-124.24, 41.73, -124.16, 41.78)
SIM_DURATION_S = 900
OUTPUT_FRAMES = 5

from trid3nt_server.agent.workflows.geoclaw.amr_regions.amr_regions import (
    geoclaw_amr_refinement_regions,
)
from trid3nt_server.agent.workflows.geoclaw.regional_manning.regional_manning import (
    geoclaw_regional_manning_friction,
)


def _prefixes() -> set[str]:
    out: set[str] = set()
    pag = s3.get_paginator("list_objects_v2")
    for page in pag.paginate(Bucket=runs_bucket):
        for obj in page.get("Contents", []) or []:
            k = obj["Key"]
            if k.startswith("case-manifests/") or k.startswith("case-views/"):
                continue
            out.add(k.split("/")[0])
    return out


def _initial_mass(prefix: str) -> str | None:
    try:
        body = s3.get_object(Bucket=runs_bucket, Key=f"{prefix}/geoclaw.stdout")[
            "Body"
        ].read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("no geoclaw.stdout for %s: %s", prefix, exc)
        return None
    hits = re.findall(r"Total mass at initial time[^\n]*", body)
    return hits[0].strip() if hits else None


def _setrun_grep(prefix: str, needles: list[str]) -> dict[str, bool]:
    """Confirm the knob reached the authored deck by grepping the uploaded setrun.py."""
    found = {n: False for n in needles}
    for key in (f"{prefix}/setrun.py", f"{prefix}/deck/setrun.py"):
        try:
            txt = s3.get_object(Bucket=runs_bucket, Key=key)["Body"].read().decode(
                "utf-8", "replace"
            )
        except Exception:  # noqa: BLE001
            continue
        for n in needles:
            found[n] = n in txt
        break
    return found


async def _one(label: str, coro, setrun_needles: list[str]) -> dict:
    pre = _prefixes()
    log.info("=== %s: invoking ===", label)
    result = await coro
    post = _prefixes()
    new = sorted(post - pre)
    r = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    rec = {
        "label": label,
        "new_prefixes": new,
        "result_type": type(result).__name__,
        "max_depth_m": r.get("max_depth_m") if isinstance(r, dict) else None,
        "max_inundation_m": r.get("max_inundation_m") if isinstance(r, dict) else None,
        "flooded_area_km2": r.get("flooded_area_km2") if isinstance(r, dict) else None,
        "arrival_time_s": r.get("arrival_time_s") if isinstance(r, dict) else None,
        "error_code": r.get("error_code") if isinstance(r, dict) else None,
        "uri": r.get("uri") if isinstance(r, dict) else None,
    }
    for p in new:
        rec["initial_mass"] = _initial_mass(p)
        rec["setrun_knob_present"] = _setrun_grep(p, setrun_needles)
    log.info("%s result: %s", label, json.dumps(rec, default=str))
    return rec


async def _main() -> int:
    records = []
    # Smoke A: regional (banded) Manning -- smooth offshore, rough onshore.
    records.append(
        await _one(
            "regional_manning",
            geoclaw_regional_manning_friction(
                bbox=BBOX,
                manning_coefficients=[0.015, 0.06],
                manning_break=[0.0],
                scenario="tsunami",
                source_magnitude=8.5,
                sim_duration_s=SIM_DURATION_S,
                output_frames=OUTPUT_FRAMES,
                amr_levels=2,
            ),
            setrun_needles=["manning_coefficient = [0.015, 0.06]", "manning_break = [0.0]"],
        )
    )
    # Smoke B: explicit AMR region window pinning a sub-box to the finest level.
    records.append(
        await _one(
            "amr_regions",
            geoclaw_amr_refinement_regions(
                bbox=BBOX,
                amr_regions=[
                    {
                        "min_level": 3,
                        "max_level": 3,
                        "t_start_s": 0.0,
                        "t_end_s": SIM_DURATION_S,
                        "min_lon": -124.21,
                        "max_lon": -124.18,
                        "min_lat": 41.745,
                        "max_lat": 41.770,
                    }
                ],
                scenario="tsunami",
                source_magnitude=8.5,
                sim_duration_s=SIM_DURATION_S,
                output_frames=OUTPUT_FRAMES,
                amr_levels=3,
            ),
            setrun_needles=["3, 3, 0.0, 900.0, -124.21, -124.18, 41.745, 41.77"],
        )
    )
    out = Path("docs/proof/geoclaw_knobs_smoke.json")
    out.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    print("\n=== SMOKE SUMMARY ===")
    print(json.dumps(records, indent=2, default=str))
    ok = all(
        r["error_code"] is None and r["new_prefixes"] for r in records
    )
    return 0 if ok else 2


sys.exit(asyncio.run(_main()))
