"""Direct ELMFIRE sensitivity-template invocation for trid3nt-local proof.

Bypasses the LLM/agent chat layer -- calls a deterministic ELMFIRE workflow
composer directly. Each sweep point runs the FULL local chain: build a constant
flat deck -> stage manifest -> run_solver('elmfire') local-docker container ->
wait_for_completion -> read rasters -> postprocess -> publish_layer (COG).

Usage (from the repo root):
  set -a; source .env.local; set +a
  PYTHONPATH=server/src:contracts/src:. \
    venvs/agent/bin/python scripts/run_elmfire_direct.py <template>

  <template> in {ltw, wind, moisture} (default: ltw).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("run_elmfire_direct")

WHICH = sys.argv[1] if len(sys.argv) > 1 else "ltw"


async def _run():
    if WHICH == "ltw":
        from trid3nt_server.agent.workflows.elmfire.sensitivity.ltw_ceiling.ltw_ceiling import (
            elmfire_length_to_width_ceiling_sensitivity as fn,
        )
        return await fn(
            max_low_min=3.0, max_low_max=12.0, n_max_low_steps=3,
            wind_speed_mph=20.0, duration_hours=0.75, cellsize_m=45.0, domain_km=12.0,
        )
    if WHICH == "wind":
        from trid3nt_server.agent.workflows.elmfire.sensitivity.wind_fluctuation.wind_fluctuation import (
            elmfire_wind_fluctuation_randomization as fn,
        )
        return await fn(
            n_members=4, ws_fluctuation_intensity=0.4, wd_fluctuation_intensity=0.15,
            wind_speed_mph=25.0, duration_hours=0.75, cellsize_m=45.0, domain_km=10.0,
        )
    if WHICH == "moisture":
        from trid3nt_server.agent.workflows.elmfire.sensitivity.live_moisture.live_moisture import (
            elmfire_live_fuel_moisture_sensitivity as fn,
        )
        return await fn(
            lh_min_pct=30.0, lh_max_pct=150.0, n_moisture_steps=3,
            wind_speed_mph=20.0, duration_hours=0.75, cellsize_m=45.0, domain_km=10.0,
        )
    raise SystemExit(f"unknown template {WHICH!r} (ltw|wind|moisture)")


result = asyncio.run(_run())
log.info("template=%s returned type=%s", WHICH, type(result).__name__)

if hasattr(result, "model_dump"):
    d = result.model_dump(mode="json")
else:
    d = result

PROOF = Path(__file__).parent.parent / "docs" / "proof" / "templates"
PROOF.mkdir(parents=True, exist_ok=True)
out = PROOF / f"elmfire_{WHICH}_direct_result.json"
out.write_text(json.dumps(d, indent=2, default=str))
log.info("summary -> %s", out)
print(json.dumps(d, indent=2, default=str)[:4000])

if isinstance(result, dict) and result.get("error_code"):
    log.error("FAILED envelope: %s", result.get("error_code"))
    sys.exit(2)
print(f"\nELMFIRE {WHICH} direct run PASSED")
