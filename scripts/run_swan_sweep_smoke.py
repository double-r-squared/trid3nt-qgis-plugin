"""Live smoke for the two SWAN CAND-S templates (direct-call, local-docker).

Runs REAL SWAN docker solves for:
  - swan_physics_sensitivity_sweep(axis="breaking_gamma", values=[0.5, 0.9])
  - swan_stationary_snapshot_batch(hs_sequence=[2.0, 4.0])
over a small Huntington Beach CA coastal box (CUDEM bathy). Each solve fetches the
DEM once, runs swanrun in a container, postprocesses the Hs field. Writes the
typed results + the per-run peak-COG s3 URIs to the scratchpad for the proof
renders. Exits non-zero if any solve failed or the sweep did not vary Hs.

Run (from repo root, full MinIO env):
  set -a; source .env.local; set +a
  sg docker -c 'PYTHONPATH=.:contracts venvs/agent/bin/python scripts/run_swan_sweep_smoke.py'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_swan_sweep_smoke")

BBOX = (-118.05, 33.60, -117.95, 33.70)  # Huntington Beach CA (Pacific open to W)
OUT = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
           "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/swan_sweep_smoke.json")


async def _main() -> int:
    from trid3nt_server.agent.workflows.swan.physics_sensitivity_sweep.physics_sensitivity_sweep import (
        swan_physics_sensitivity_sweep,
    )
    from trid3nt_server.agent.workflows.swan.stationary_snapshot_batch.stationary_snapshot_batch import (
        swan_stationary_snapshot_batch,
    )

    log.info("backend=%s image=%s endpoint=%s",
             os.environ.get("TRID3NT_SOLVER_BACKEND"),
             os.environ.get("TRID3NT_SWAN_IMAGE"),
             os.environ.get("AWS_ENDPOINT_URL"))

    log.info("=== sweep: breaking_gamma [0.4, 0.9] ===")
    sweep = await swan_physics_sensitivity_sweep(
        bbox=BBOX, axis="breaking_gamma", values=[0.4, 0.9])
    log.info("sweep result: %s", json.dumps(sweep, default=str)[:1500])

    log.info("=== snapshot batch: hs_sequence [2.0, 4.0] ===")
    batch = await swan_stationary_snapshot_batch(
        bbox=BBOX, hs_sequence=[2.0, 4.0])
    log.info("batch result: %s", json.dumps(batch, default=str)[:1500])

    OUT.write_text(json.dumps({"sweep": sweep, "batch": batch}, indent=2, default=str))
    log.info("wrote %s", OUT)

    # Gates: both ok; the sweep must actually VARY the field (knob is real).
    rc = 0
    if sweep.get("status") != "ok":
        log.error("sweep FAILED: %s", sweep.get("error_message")); rc = 2
    else:
        areas = [s["wave_area_km2"] for s in sweep["schemes"]]
        hs = [s["max_hs_m"] for s in sweep["schemes"]]
        log.info("sweep areas=%s max_hs=%s", areas, hs)
        if len(set(round(a, 4) for a in areas)) < 2 and len(set(round(h, 4) for h in hs)) < 2:
            log.error("sweep did NOT vary the field across breaking_gamma -- knob not demonstrated")
            rc = 3
    if batch.get("status") != "ok":
        log.error("batch FAILED: %s", batch.get("error_message")); rc = rc or 4
    else:
        log.info("batch peak_hs=%s", [s["max_hs_m"] for s in batch["snapshots"]])
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
