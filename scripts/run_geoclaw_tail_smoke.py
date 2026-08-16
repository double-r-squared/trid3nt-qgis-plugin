"""Live smoke for the GeoClaw CAND-S tail folds (ADR 0155): Lagrangian particle
gauges + the onshore fgmax mask.

Two direct-call solves against the local-docker GeoClaw image (Crescent City, CA):
  A) tsunami + lagrangian_particles (3 harbour drifters), fgmax_mask='full'
     -> particle drift tracks (particles.geojson) + full fgmax point count.
  B) tsunami + fgmax_mask='onshore'
     -> onshore fgmax point count (the output-size win) + max-depth agreement.

Reports, per run: the MinIO run prefix, depth scalars, the 'Total mass at initial
time' diagnostic (~1e5 == no wave, ~1e9+ == a real wave), particle track
lengths/duration, and full-vs-onshore fgmax point counts + max-depth agreement.
Cheap: coarse grid, short window.

Run (from repo root):
  set -a; source .env.local; set +a
  export TMPDIR=/home/nate/.cache/geoclaw_smoke_tmp && mkdir -p "$TMPDIR"
  PYTHONPATH=.:contracts \
    venvs/agent/bin/python scripts/run_geoclaw_tail_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("geoclaw_tail_smoke")

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
# Three drifters seeded in the harbour (wet cells) to trace the wake.
HARBOUR_PARTICLES = [(-124.187, 41.744), (-124.190, 41.746), (-124.184, 41.748)]
SIM_DURATION_S = 900
OUTPUT_FRAMES = 5
AMR_LEVELS = 3

from trid3nt_server.workflows.geoclaw.inundation.inundation import geoclaw_inundation


def _prefixes() -> set[str]:
    out: set[str] = set()
    pag = s3.get_paginator("list_objects_v2")
    for page in pag.paginate(Bucket=runs_bucket):
        for o in page.get("Contents", []) or []:
            out.add(o["Key"].split("/", 1)[0])
    return out


def _new_prefixes(before: set[str]) -> list[str]:
    return sorted(_prefixes() - before)


def _find_across(prefixes: list[str], suffix: str) -> str | None:
    """Find a key ending in ``suffix`` across any of the given run prefixes."""
    for p in prefixes:
        r = s3.list_objects_v2(Bucket=runs_bucket, Prefix=f"{p}/")
        for o in r.get("Contents", []) or []:
            if o["Key"].endswith(suffix):
                return o["Key"]
    return None


def _total_mass(prefixes: list[str]) -> str:
    key = _find_across(prefixes, "geoclaw.stdout")
    if key is None:
        return "?"
    txt = s3.get_object(Bucket=runs_bucket, Key=key)["Body"].read().decode("utf-8", "replace")
    m = re.findall(r"Total mass at initial time:\s*([0-9.eE+\-]+)", txt)
    return m[0] if m else "?"


def _fgmax_points(prefixes: list[str]) -> tuple[int, float, float]:
    """Return (n_points, max_h, max_land_h) from fgmax0001.txt (9-col)."""
    key = _find_across(prefixes, "fgmax0001.txt")
    if key is None:
        return 0, 0.0, 0.0
    txt = s3.get_object(Bucket=runs_bucket, Key=key)["Body"].read().decode("utf-8", "replace")
    n = 0
    max_h = 0.0
    max_land_h = 0.0
    for line in txt.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) != 9:
            continue
        try:
            B = float(parts[3])
            h = float(parts[4])
        except ValueError:
            continue
        n += 1
        if h > -1e50:
            max_h = max(max_h, h)
            if B > 0.0:
                max_land_h = max(max_land_h, h)
    return n, max_h, max_land_h


async def _run(**kw):
    before = _prefixes()
    res = await geoclaw_inundation(
        bbox=BBOX,
        scenario="tsunami",
        sim_duration_s=SIM_DURATION_S,
        output_frames=OUTPUT_FRAMES,
        amr_levels=AMR_LEVELS,
        source_magnitude=9.0,
        **kw,
    )
    prefixes = _new_prefixes(before)
    return res, prefixes


async def main() -> int:
    print("\n===== RUN A: Lagrangian particles + full fgmax =====")
    resA, prefA = await _run(lagrangian_particles=HARBOUR_PARTICLES)
    if not hasattr(resA, "max_depth_m"):
        print("RUN A FAILED:", resA)
        return 1
    print(f"new_prefixes={prefA}  peak_uri={resA.uri}  total_mass_init={_total_mass(prefA)}")
    print(
        f"  max_depth_m={resA.max_depth_m:.4g} flooded_km2={resA.flooded_area_km2:.4g} "
        f"max_inundation_m={resA.max_inundation_m:.4g} arrival_s={resA.arrival_time_s}"
    )
    print(
        f"  particle_track_count={resA.particle_track_count} "
        f"max_track_len_m={resA.particle_max_track_length_m} "
        f"duration_s={resA.particle_track_duration_s}"
    )
    pkey = _find_across(prefA, "particles.geojson")
    if pkey:
        print(f"  particles.geojson key: {pkey}")
        fc = json.loads(s3.get_object(Bucket=runs_bucket, Key=pkey)["Body"].read())
        print(f"  particles.geojson: {len(fc['features'])} tracks; per-track:")
        for f in fc["features"]:
            p = f["properties"]
            print(
                f"    gauge {p['gauge_id']}: n_pts={p['n_points']} "
                f"len={p['track_length_m']} m dur={p['duration_s']} s"
            )
    else:
        print("  particles.geojson: MISSING")
    nA, hA, landA = _fgmax_points(prefA)
    print(f"  fgmax(full): points={nA} max_h={hA:.4g} max_land_h={landA:.4g}")

    print("\n===== RUN B: onshore fgmax mask =====")
    resB, prefB = await _run(fgmax_mask="onshore")
    if not hasattr(resB, "max_depth_m"):
        print("RUN B FAILED:", resB)
        return 1
    print(f"new_prefixes={prefB}  total_mass_init={_total_mass(prefB)}")
    print(
        f"  max_depth_m={resB.max_depth_m:.4g} max_inundation_m={resB.max_inundation_m:.4g} "
        f"arrival_s={resB.arrival_time_s}"
    )
    nB, hB, landB = _fgmax_points(prefB)
    print(f"  fgmax(onshore): points={nB} max_h={hB:.4g} max_land_h={landB:.4g}")

    print("\n===== SUMMARY =====")
    print(f"fgmax points  full={nA}  onshore={nB}  reduction={100*(1-nB/max(nA,1)):.1f}%")
    print(f"onshore max_h={hB:.4g} vs full max_land_h={landA:.4g} (should agree)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
