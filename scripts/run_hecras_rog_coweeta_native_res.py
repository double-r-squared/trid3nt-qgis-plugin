"""Direct hecras_flood_2d rain-on-grid invocation at the solver's declared floor.

Fidelity-first proof drive (NATE ask, 2026-08-11): resolution_m=20 is the
flood2d/HEC-RAS 2025 mesh generator's declared MINIMUM (_MIN_RES_M in
flood_2d.py) -- the SOLVER floor, distinct from the 3DEP DEM's 10 m NATIVE
source resolution (the mesh coarsens the 10 m terrain to a 20 m subgrid).

AOI: a ~6x6 km box inside the Coweeta bbox [-83.47, 35.02, -83.36, 35.1]
containing the main channel.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  PYTHONPATH=server/src:contracts/src:. venvs/agent/bin/python \
    scripts/run_hecras_rog_coweeta_native_res.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_hecras_rog_coweeta_native_res")

# A first attempt used a ~6x6 km box, but the granularity autoscaler (soft cap
# 12000 cells, ADR 0223) silently coarsened resolution_m=20 -> 61m for that AOI
# size. To actually run the SOLVER at its declared 20 m floor (the NATE ask), the
# box is shrunk to fit under the cap: ~3.2x1.4 km centered on the Coweeta outlet
# reach (near 35.0601 N, -83.4306 W), 160x70 cells = 11200 < 12000 cap.
BBOX = (-83.4482, 35.0538, -83.4131, 35.0664)
RESOLUTION_M = 20.0  # solver-floor (mesh generator MIN); 3DEP source is 10 m native

from trid3nt_server.agent.tools import TOOL_REGISTRY  # noqa: E402

PROOF_DIR = Path(__file__).parent.parent / "docs" / "proof"
PROOF_DIR.mkdir(parents=True, exist_ok=True)


async def main():
    fn = TOOL_REGISTRY["hecras_flood_2d"].fn
    t0 = time.time()
    result = await fn(
        bbox=list(BBOX),
        resolution_m=RESOLUTION_M,
        design_storm_mm_per_hr=25.0,
        storm_duration_hr=6.0,
        channel_refinement=None,
    )
    wall = time.time() - t0
    j = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    out = PROOF_DIR / "hecras_rog_coweeta_native_result.json"
    out.write_text(json.dumps(j, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s wall=%.1fs", out, wall)
    print(json.dumps(j, indent=2, default=str)[:3000])
    print(f"\nWALL_S={wall:.1f}")
    if isinstance(result, dict) and result.get("status") == "error":
        raise SystemExit(f"FAILED: {result.get('error_code')} {result.get('error_message')}")


if __name__ == "__main__":
    asyncio.run(main())
