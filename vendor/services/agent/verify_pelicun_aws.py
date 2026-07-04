"""Deterministic Track-A acceptance on the live AWS stack (no Bedrock, no agent).

Invokes compute_impact_envelope on a REAL flood-depth COG from a prior run,
deriving the lon/lat bbox from the COG itself so the live NSI fetch overlaps the
flooded area. Proves: s3:// flood-COG read + s3:// NSI fetch + Pelicun HAZUS solve
+ postprocess + the job-0300 fields (n_assets_default_replacement_value) on AWS.
"""
from __future__ import annotations

import asyncio
import json
import sys

import rasterio
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds

from grace2_agent.tools.cache import read_object_bytes_s3
from grace2_agent.workflows.compute_impact_envelope import compute_impact_envelope

COG = sys.argv[1] if len(sys.argv) > 1 else (
    "s3://grace2-hazard-runs-226996537797/01KTX3D7Q7RY02ASJK8ZF5C86V/flood_depth_peak.tif"
)


def _lonlat_bbox(cog_uri: str) -> list[float]:
    with MemoryFile(read_object_bytes_s3(cog_uri)) as mf, mf.open() as src:
        b = src.bounds
        crs = src.crs
    if crs and crs.to_epsg() != 4326:
        b = transform_bounds(crs, "EPSG:4326", *b)
    return [float(b[0]), float(b[1]), float(b[2]), float(b[3])]


async def main() -> int:
    bbox = _lonlat_bbox(COG)
    print(f"flood COG: {COG}")
    print(f"derived lon/lat bbox: {bbox}")
    res = await compute_impact_envelope(flood_layer_uri=COG, bbox=bbox)
    env = res.get("raw_envelope") if isinstance(res, dict) else None
    if env is None:
        print("RESULT (no raw_envelope):", json.dumps(res, default=str)[:800])
        return 1
    keys = [
        "n_structures_total", "n_structures_damaged", "n_structures_destroyed",
        "total_replacement_value_usd", "expected_loss_usd", "loss_percentile_95_usd",
        "population_total", "population_displaced",
        "n_assets_default_replacement_value", "fragility_set", "realization_count",
        "structure_inventory_source", "pelicun_run_id",
    ]
    print("=== ImpactEnvelope (live AWS) ===")
    for k in keys:
        print(f"  {k}: {env.get(k)}")
    print("  damage_state_distribution:", env.get("damage_state_distribution"))
    ok = (
        env.get("n_structures_total", 0) > 0
        and "n_assets_default_replacement_value" in env
        and env.get("pelicun_run_id")
    )
    print("PASS" if ok else "REVIEW (no structures in flood extent?)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
