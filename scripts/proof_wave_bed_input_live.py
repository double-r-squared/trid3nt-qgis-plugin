#!/usr/bin/env python3
"""Live E2E smoke: the in-worker lake-datum bed surfaces as a Case INPUT layer.

Drives the lake-datum ARTEMIS composer (Marquette Lower Harbor breakwater
diffraction) through the rebuilt worker image with a capturing emitter, and
asserts it surfaces its sampled bed as a role=context "Input: lake bed
bathymetry (...)" raster whose georeferenced bounds land INSIDE the request AOI
(the ARTEMIS Gulf-of-Guinea georef bug is the cautionary tale -- bounds are
checked numerically). ASCII only.

Run: set -a; source .env.local; set +a; \
     venvs/agent/bin/python scripts/proof_wave_bed_input_live.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_fidelity_proof_generic import download_s3  # noqa: E402

import trid3nt_server.tools as _bootstrap  # noqa: F401,E402 -- init the tool registry first
from trid3nt_contracts import new_ulid  # noqa: E402
from trid3nt_server.emission.pipeline_emitter import (  # noqa: E402
    _CURRENT_EMITTER,
    PipelineEmitter,
)
from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402


async def _capture_sink(text: str) -> None:
    json.loads(text)  # validate framing only


def _bed_rows(emitter: PipelineEmitter) -> list:
    return [r for r in emitter._loaded_layers
            if (r.name or "").startswith("Input: lake bed bathymetry (")]


def _bounds_inside(uri: str, aoi: tuple, pad: float = 0.05) -> tuple:
    local = download_s3(uri)
    with rasterio.open(local) as src:
        b = src.bounds
        epsg = src.crs.to_epsg()
    inside = (aoi[0] - pad <= b.left and aoi[1] - pad <= b.bottom
              and b.right <= aoi[2] + pad and b.top <= aoi[3] + pad)
    return inside, (round(b.left, 4), round(b.bottom, 4), round(b.right, 4),
                    round(b.top, 4)), epsg


async def _drive_artemis() -> int:
    fn = TOOL_REGISTRY["artemis_harbor_agitation"].fn
    emitter = PipelineEmitter(session_id=new_ulid(), sink=_capture_sink)
    aoi = (-87.392, 46.528, -87.368, 46.55)
    token = _CURRENT_EMITTER.set(emitter)
    try:
        out = await fn(
            location=None, bbox=aoi, wave_mode="diffraction",
            wave_period_s=8.0, wave_direction_deg=129.2, wave_height_m=2.0,
            reflection_coef=0.5, breakwater=None, target_resolution_m=30.0,
            bathy_source="noaa_greatlakes")
    finally:
        _CURRENT_EMITTER.reset(token)
    if isinstance(out, dict):
        print("[artemis] ERROR:", out)
        return 1
    print("[artemis] result uri:", out.uri, "kd_max:", getattr(out, "kd_max", None))
    rows = _bed_rows(emitter)
    assert rows, "artemis surfaced NO lake-bed input row"
    row = rows[0]
    print("[artemis] bed input:", row.name)
    print("[artemis] bed uri  :", row.uri, "role:", row.role,
          "preset:", row.style_preset)
    assert row.role == "context" and row.style_preset == "continuous_dem"
    inside, bnds, epsg = _bounds_inside(row.uri, aoi, pad=0.02)
    print(f"[artemis] bed COG bounds={bnds} epsg={epsg} inside_AOI={inside}")
    assert epsg == 4326, f"bed COG not 4326: {epsg}"
    assert inside, "artemis bed COG escaped the AOI (Gulf-of-Guinea georef regression)"
    return 0


async def _run() -> int:
    rc = await _drive_artemis()
    print("SMOKE OK" if rc == 0 else "SMOKE FAILED")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
