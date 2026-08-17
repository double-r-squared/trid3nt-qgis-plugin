"""deep-water run-up LIVE proof driver (direct-call, local-docker).

Re-drives the REAL 2021 M8.2 Chignik finite-fault tsunami NOW that the deep-water
rung (ETOPO full-column no longer clobbered by the 3DEP land ocean-fill) lets the
GEOCLAW_BATHYMETRY_FLAT guard pass. Places a nearshore coastal gauge + an fgout
smooth-field monitor so the offshore waveform (amplitude + arrival) is recorded even
where the modest ~1 m event produces little overland inundation on the 450 m coast.

Run (repo root, MinIO env + a raised solver timeout):
  set -a; source .env.local; set +a
  TRID3NT_SOLVER_TIMEOUT_S=5400 venvs/agent/bin/python \
    scripts/drive_geoclaw_chignik_runup_proof.py
"""
from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

_AK_PENINSULA = (-159.8, 55.0, -158.8, 55.6)
_GAUGE = (-159.30, 55.30)  # nearshore shelf point in the AOI (~ -180 m)


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
        coastal_gauge_lonlat=_GAUGE,
        sim_duration_s=1800.0,
        amr_levels=3,
        output_frames=12,
        fgout_frames=15,
        compute_class="standard",
    )
    print("=== RESULT ===")
    if hasattr(res, "max_depth_m"):
        print("status: ok")
        print("run_id/uri:", res.uri)
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


if __name__ == "__main__":
    asyncio.run(_main())
