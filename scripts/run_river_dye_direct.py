"""Direct ``telemac_river_dye`` invocation - the dye-plume reference run.

Bypasses the LLM/chat layer: calls the registered template directly and prints
the PHYSICAL ANSWER (peak concentration + its arrival time + how far the plume
reached) as one JSON line prefixed ``PHYSICAL_ANSWER ``, so an old-vs-new
migration comparison is a diff of two lines.

The carrier discharge normally resolves from the NOAA National Water Model at
the reach. NWM publishes only recent ``analysis_assim`` cycles, so a reference
run PINS it (``--discharge-m3s``) - a value that moves between two runs is not a
parity test.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/run_river_dye_direct.py --tag old --discharge-m3s 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("run_river_dye_direct")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The committed TELEMAC showcase reach - a real NHDPlus reach WITH NHDArea
#: polygon coverage (the ``bank_source="nhd_area"`` precondition). Natural place
#: name, no bbox.
LOCATION = "Eel River near Scotia, California"
ARGS: dict = {
    "location": LOCATION,
    "substance": "dye",
    "spill_fraction": 0.25,
    "spill_duration_s": 300.0,
    "dye_concentration_mgl": 100.0,
    "reach_length_km": 6.0,
    "sim_duration_s": 3600.0,
    "source_q_m3s": 8.0,
    "channel_width_m": 60.0,
    "mesh_resolution": "auto",
}


def _physical_answer(layer) -> dict:
    """The comparable physics: peak concentration, when it peaks, how far it got."""
    get = (lambda f: getattr(layer, f, None)) if not isinstance(layer, dict) \
        else layer.get
    return {
        "dye_cmax_mgl": get("dye_cmax_mgl"),
        "dye_peak_time_s": get("dye_peak_time_s"),
        "plume_reach_m": get("plume_reach_m"),
        "active_frames": get("active_frames"),
        "mesh_size_m": get("mesh_size_m"),
        "mesh_node_estimate": get("mesh_node_estimate"),
        "mesh_resolution_label": get("mesh_resolution_label"),
        "bbox": list(get("bbox")) if get("bbox") else None,
        "uri": get("uri"),
        "layer_id": get("layer_id"),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--out", default=None)
    ap.add_argument("--discharge-m3s", type=float, default=None)
    ap.add_argument("--substance", default=None)
    ap.add_argument("--input-mode", default=None)
    args = ap.parse_args()

    log.info("backend=%s telemac_image=%s runs_bucket=%s endpoint=%s",
             os.environ.get("TRID3NT_SOLVER_BACKEND"),
             os.environ.get("TRID3NT_TELEMAC_IMAGE"),
             os.environ.get("TRID3NT_RUNS_BUCKET"),
             os.environ.get("AWS_ENDPOINT_URL"))

    from trid3nt_server.data import TOOL_REGISTRY  # import populates the registry

    call = dict(ARGS)
    if args.discharge_m3s is not None:
        call["discharge_m3s"] = args.discharge_m3s
    if args.substance is not None:
        call["substance"] = args.substance
    if args.input_mode is not None:
        call["input_mode"] = args.input_mode

    fn = TOOL_REGISTRY["telemac_river_dye"].fn
    log.info("invoking telemac_river_dye %s", call)
    out = await fn(**call)

    if isinstance(out, dict) and out.get("status") == "error":
        log.error("FAILED %s: %s", out.get("error_code"), out.get("error_message"))
        print("PHYSICAL_ANSWER " + json.dumps({"error": out}))
        return 1

    ans = _physical_answer(out)
    ans["tag"] = args.tag
    line = "PHYSICAL_ANSWER " + json.dumps(ans, default=str)
    print(line)
    if args.out:
        Path(args.out).write_text(json.dumps(ans, indent=2, default=str))
    log.info("status=ok %s", ans)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
