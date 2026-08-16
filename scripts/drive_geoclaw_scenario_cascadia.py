"""ADR 0230 Slab2 SCENARIO live driver (direct-call, local-docker): a Cascadia M9.0
"what if" tsunami end to end.

The scenario rung: NOT a real catalog event -- a HYPOTHETICAL full-margin Cascadia
rupture whose GEOMETRY is the real USGS Slab2 subduction interface (depth/strike/dip),
tiled into subfaults that follow the CURVED trench, with a Strasser-2010-scaled,
Tukey-tapered slip summing to M9.0. Drives a multi-subfault Okada deformation ->
dtopo -> the deep-water bathymetry (ADR 0229 rung) -> GeoClaw solve -> coastal
amplitude on the Washington/Oregon coast.

ScienceBase is Cloudflare-walled from this datacenter (the production
fetch_slab2_grids children-API path is exercised by the monkeypatched offline test);
so this driver PRE-SEEDS the Slab2 grid cache with a Cascadia interface grounded in the
real trench geometry (convex-west trench, ~11 deg ENE dip, depth to 60 km) -- the
curvature is genuine, which is what the deformation proof demonstrates. Set
TRID3NT_SLAB2_LIVE=1 to skip the seed and use the real ScienceBase fetch when reachable.

Run (repo root, MinIO env + a raised solver timeout):
  set -a; source .env.local; set +a
  TRID3NT_SOLVER_TIMEOUT_S=7200 venvs/agent/bin/python \
    scripts/drive_geoclaw_scenario_cascadia.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

# A Washington/Oregon coastal AOI (the composer expands the domain to ENCLOSE the
# rupture footprint; this seeds the onshore run-up focus). Newport, Oregon coast.
_AOI = (-124.15, 44.45, -123.95, 44.80)
_GAUGE = (-124.10, 44.62)  # nearshore point off Newport, Oregon
_EPI = (-125.5, 45.0)      # rupture center offshore central Oregon


def _seed_slab2_cache() -> str:
    """Pre-seed the Slab2 grid cache with the real-geometry Cascadia fixture (the
    ScienceBase wall workaround). Returns the cache dir."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from _slab2_fixture import write_cascadia_fixture

    cache_root = os.environ.get("TRID3NT_CACHE_DIR") or os.path.join(
        os.path.dirname(__file__), "..", "scratchpad", "slab2_cache")
    cache_root = os.path.abspath(cache_root)
    os.environ["TRID3NT_CACHE_DIR"] = cache_root
    cas_dir = os.path.join(cache_root, "slab2", "cas")
    write_cascadia_fixture(cas_dir)
    logging.info("seeded Slab2 Cascadia fixture into %s", cas_dir)
    return cas_dir


async def _main():
    if os.environ.get("TRID3NT_SLAB2_LIVE") != "1":
        _seed_slab2_cache()

    from trid3nt_server.workflows.geoclaw.inundation.inundation import (
        geoclaw_inundation,
    )
    res = await geoclaw_inundation(
        bbox=_AOI,
        scenario_fault="Cascadia",
        scenario_magnitude=9.0,
        scenario_epicenter_lonlat=_EPI,
        target_resolution_m=25000.0,
        coastal_gauge_lonlat=_GAUGE,
        # 1800 s (30 min sim): the near-coast Cascadia source puts the wave on the
        # Oregon coast within ~10-20 min, so the coastal gauge peak + the near-field
        # offshore decay/arrival transect are fully captured; a longer sim only adds
        # far-field oscillation the amplitude deliverables do not need. The wall cost
        # is the geometric AMR depth (a tiny coastal AOI inside the 6x6 deg full-margin
        # domain forces ~5 refinement levels), NOT the (now coarse) basin bathy.
        sim_duration_s=1800.0,
        amr_levels=2,
        output_frames=10,
        fgout_frames=15,
        compute_class="standard",
    )
    print("=== RESULT ===")
    if hasattr(res, "max_depth_m"):
        print("status: ok")
        print("uri:", res.uri)
        print("scenario:", res.scenario)
        print("max_depth_m:", res.max_depth_m)
        print("max_inundation_m:", res.max_inundation_m)
        print("flooded_area_km2:", res.flooded_area_km2)
        print("source_note:", res.source_note)
        for si in (res.synthetic_inputs or []):
            print(f"  provenance: {si.param} basis={si.basis} value={si.value!r}")
    else:
        print("status: error")
        print(res)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
