"""Direct landlab_groundwater_water_table invocation at 3DEP-native 10 m resolution.

Fidelity-first proof drive (NATE ask, 2026-08-11): the template default
target_resolution_m is 30 m; this passes the explicit 3DEP-native 10 m value
(basis=user) on the small Panola-adjacent showcase box.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  PYTHONPATH=src:contracts/src:. venvs/agent/bin/python \
    scripts/run_landlab_groundwater_native_res.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_landlab_groundwater_native_res")

BBOX = (-84.18, 33.60, -84.14, 33.64)
TARGET_RESOLUTION_M = 10.0  # 3DEP native (template default is 30.0)

from trid3nt_server.agent.tools import TOOL_REGISTRY  # noqa: E402

PROOF_DIR = Path(__file__).parent.parent / "docs" / "proof"
PROOF_DIR.mkdir(parents=True, exist_ok=True)


async def main():
    fn = TOOL_REGISTRY["landlab_groundwater_water_table"].fn
    t0 = time.time()
    result = await fn(
        bbox=list(BBOX),
        target_resolution_m=TARGET_RESOLUTION_M,
        compute_class="small",
    )
    wall = time.time() - t0
    j = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    out = PROOF_DIR / "landlab_groundwater_water_table_native_result.json"
    out.write_text(json.dumps(j, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s wall=%.1fs", out, wall)
    print(json.dumps(j, indent=2, default=str)[:3000])
    print(f"\nWALL_S={wall:.1f}")
    if isinstance(result, dict) and result.get("status") == "error":
        raise SystemExit(f"FAILED: {result.get('error_code')} {result.get('error_message')}")


if __name__ == "__main__":
    asyncio.run(main())
