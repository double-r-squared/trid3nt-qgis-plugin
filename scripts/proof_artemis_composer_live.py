#!/usr/bin/env python3
"""Live E2E smoke of the wired ARTEMIS composer through the product path.

Direct-call (emitter=None) of artemis_harbor_agitation on the Marquette Lower
Harbor AOI: exercises the OSM breakwater auto-fetch -> agitation manifest with
breakwater_polylines -> run_solver (local docker, the rebuilt image meshes the
REAL structure) -> postprocess with the latent-#7 georef fix -> published Kd COG.
Asserts the published layer georeferences INSIDE the AOI (the regression the fix
enforces). ASCII only.
"""
from __future__ import annotations

import asyncio


async def _run():
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    fn = TOOL_REGISTRY["artemis_harbor_agitation"].fn
    aoi = [-87.392, 46.528, -87.368, 46.55]
    out = await fn(
        bbox=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes",
        wave_height_m=2.0, wave_period_s=8.0, wave_direction_deg=129.2,
        reflection_coef=0.5, target_resolution_m=30.0)
    if isinstance(out, dict):
        print("ERROR:", out)
        return 1
    print("layer_id   :", out.layer_id)
    print("uri        :", out.uri)
    print("kd_max     :", out.kd_max)
    print("kd_shelter :", out.kd_sheltered)
    print("kd_exposed :", out.kd_exposed)
    print("bbox       :", out.bbox)
    b = out.bbox
    inside = (aoi[0] - 0.01 <= b[0] and aoi[1] - 0.01 <= b[1]
              and b[2] <= aoi[2] + 0.01 and b[3] <= aoi[3] + 0.01)
    print("GEOREF bbox inside AOI:", inside)
    assert inside, "published COG escaped the AOI -- latent #7 georef regression"
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
