"""Direct-call (headless, no LLM/WS) coarsened tsunami re-run on the REAL 2021 M8.2
Chignik finite-fault source -- the finite-fault-upgrade live proof.

Exercises the FULL real path: earthquake_source resolve (USGS ComCat) -> finite-fault
product fetch (ak0219neiszm_1) -> normalized CSV stage -> domain enclosure of the
rupture footprint -> topobathy fetch -> local-docker GeoClaw solve on the N-subfault
Okada dtopo -> postprocess (peak depth + deformation product). Coarsened explicitly
(short sim, 2 AMR levels) per the proof norm.

Run (from repo root, MinIO env loaded):
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/drive_geoclaw_finite_fault_chignik.py
"""
from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

_AK_PENINSULA = (-159.8, 55.0, -158.8, 55.6)


async def _main():
    from trid3nt_server.workflows.geoclaw.inundation.inundation import (
        geoclaw_inundation,
    )
    res = await geoclaw_inundation(
        bbox=_AK_PENINSULA,
        earthquake_source="Alaska Peninsula",
        earthquake_min_magnitude=8.0,
        earthquake_start_date="2021-07-01",
        earthquake_end_date="2021-08-15",
        sim_duration_s=600.0,
        amr_levels=2,
        output_frames=4,
        compute_class="small",
    )
    print("=== RESULT ===")
    if hasattr(res, "max_depth_m"):
        print("status: ok")
        print("scenario:", res.scenario)
        print("max_depth_m:", res.max_depth_m)
        print("max_inundation_m:", res.max_inundation_m)
        print("flooded_area_km2:", res.flooded_area_km2)
        print("source_note:", res.source_note)
        for si in (res.synthetic_inputs or []):
            print(f"  provenance: {si.param} basis={si.basis} value={si.value!r}")
        print("uri:", res.uri)
    else:
        print("status: error")
        print(res)


if __name__ == "__main__":
    asyncio.run(_main())
