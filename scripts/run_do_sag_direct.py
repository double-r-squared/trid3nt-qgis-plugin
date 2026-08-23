"""Direct ``telemac_do_sag`` invocation - the DO-sag reference run.

Bypasses the LLM/chat layer: calls the registered template directly and prints
the PHYSICAL ANSWER (sag minimum value + its downstream location + the standard
verdict) as one JSON line prefixed ``PHYSICAL_ANSWER ``, so an old-vs-new
migration comparison is a diff of two lines.

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) PYTHONPATH=.:contracts \\
    venvs/agent/bin/python scripts/run_do_sag_direct.py [--tag NAME]
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
log = logging.getLogger("run_do_sag_direct")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# A real NHDPlus reach WITH NHDArea polygon coverage (the bank_source="nhd_area"
# precondition) - the committed TELEMAC showcase reach. Natural place name, no bbox.
LOCATION = "Eel River near Scotia, California"
ARGS: dict = {
    "location": LOCATION,
    "discharge_bod_mgl": 20.0,
    "water_temp_c": 20.0,
    "do_standard_mgl": 5.0,
    "k1_per_day": 0.3,
    "k2_per_day": 0.9,
    "reach_length_km": 12.0,
    "mesh_resolution": "auto",
}


def _physical_answer(layer) -> dict:
    """The comparable physics: sag minimum, its location, the verdict."""
    get = (lambda f: getattr(layer, f, None)) if not isinstance(layer, dict) \
        else layer.get
    curve = get("sag_curve_do_mgl") or []
    return {
        "do_min_mgl": get("do_min_mgl"),
        "do_min_distance_m": get("do_min_distance_m"),
        "do_standard_mgl": get("do_standard_mgl"),
        "do_violates_standard": get("do_violates_standard"),
        "sag_curve_points": len(curve),
        "sag_curve_first_mgl": curve[0] if curve else None,
        "sag_curve_last_mgl": curve[-1] if curve else None,
        "uri": get("uri"),
        "layer_id": get("layer_id"),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    log.info("backend=%s telemac_image=%s runs_bucket=%s endpoint=%s",
             os.environ.get("TRID3NT_SOLVER_BACKEND"),
             os.environ.get("TRID3NT_TELEMAC_IMAGE"),
             os.environ.get("TRID3NT_RUNS_BUCKET"),
             os.environ.get("AWS_ENDPOINT_URL"))

    from trid3nt_server.data import TOOL_REGISTRY  # import populates the registry

    fn = TOOL_REGISTRY["telemac_do_sag"].fn
    log.info("invoking telemac_do_sag %s", ARGS)
    out = await fn(**ARGS)

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
