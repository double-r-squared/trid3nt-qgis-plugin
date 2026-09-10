#!/usr/bin/env python3
"""The ARTEMIS ``structure`` slot, proved in all THREE of the ways it can be filled.

The slot is producer-less by design - the template names no default source for
somebody's breakwater - so it is filled by a handed-over layer, by a drawn line,
or by nobody, and the claim is that the SOLVE cannot tell them apart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402
from trid3nt_server.testing.proof_paths import packet_dir  # noqa: E402

#: Marquette Lower Harbor, MI - the canary AOI, and a real Great Lakes harbour
#: with a surveyed OSM breakwater across its approach.
BBOX = [-87.39234, 46.52812, -87.36788, 46.55021]

_BASE = {
    "bbox": BBOX,
    "wave_mode": "diffraction",
    "wave_period_s": 8.0,
    # The heading the canary declares - swell up the harbour's south-east mouth,
    # propagating north-north-west in the trig convention the param states. The
    # three ways of filling the slot are only comparable against each other and
    # against the canary if all four ask the same wave.
    "wave_direction_deg": 110.0,
    "wave_height_m": 2.0,
    "bathy_source": "noaa_greatlakes",
    "target_resolution_m": 40.0,
    "compute_class": "medium",
    # user_gated, like the canary: reflection_coef is a declared scenario default
    # with no fetcher behind it, and law 9 refuses an AUTO run that would seat an
    # invented physics value. The gate is answered "proceed", which approves the
    # labeled default explicitly - that IS the mechanism working.
    "input_mode": "user_gated",
}


def fetch_structure() -> tuple[str, list]:
    """The surveyed breakwater, through the ROUTER - uri plus its raw lines.
    Direct-call on purpose: this is a data fetch, and driving the model to make
    it would prove the model's routing rather than the slot's contract."""
    from trid3nt_server.tools import TOOL_REGISTRY

    layer = TOOL_REGISTRY["fetch_osm_breakwaters"].fn(bbox=tuple(BBOX))
    uri = getattr(layer, "uri", None) or layer["uri"]
    from trid3nt_server.workflows.shared.supplied_geometry import supplied_polylines

    return uri, supplied_polylines(uri)


def _run(name: str, args: dict, title: str) -> dict:
    ev = run_live(LiveRun(tool="artemis_harbor_agitation", args=args,
                          case_title=title, answers=GateAnswers(confirm="proceed"),
                          timeout_s=1800.0, cleanup_case=True))
    d = ev.as_dict()
    answer = d.get("metrics") or {}
    out = {
        "mode": name,
        "run_id": d.get("run_id"),
        "step_state": d.get("step_state"),
        "layers": d.get("layers"),
        "kd_max": answer.get("kd_max"),
        "kd_sheltered": answer.get("kd_sheltered"),
        "kd_exposed": answer.get("kd_exposed"),
        "hs_max_m": answer.get("hs_max_m"),
        "mesh_size_m": answer.get("mesh_size_m"),
        "structure": answer.get("structure"),
        "structure_note": answer.get("structure_note"),
    }
    print(json.dumps(out, indent=2, default=str))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="all",
                    choices=("all", "fetched", "drawn", "omitted"))
    ns = ap.parse_args()
    results = []

    if ns.mode in ("all", "fetched", "drawn"):
        uri, lines = fetch_structure()
        print(f"fetch_osm_breakwaters -> {uri} ({len(lines)} line(s), "
              f"{sum(len(l) for l in lines)} vertices)")
    if ns.mode in ("all", "fetched"):
        results.append(_run("fetched", {**_BASE, "structure": uri},
                            "artemis structure slot: FETCHED (by layer handle)"))
    if ns.mode in ("all", "drawn"):
        # The longest fetched way, handed back as a SKETCH: the same barrier by a
        # different door. A draw gate's reply is this shape and so is a typed one.
        drawn = max(lines, key=len)
        results.append(_run("drawn", {**_BASE, "structure": drawn},
                            "artemis structure slot: DRAWN (sketched polyline)"))
    if ns.mode in ("all", "omitted"):
        results.append(_run("omitted", dict(_BASE),
                            "artemis structure slot: OMITTED (open water, labeled)"))

    out = os.path.join(packet_dir("artemis_harbor_agitation"),
                       "artemis_harbor_agitation_structure_slot_evidence.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"bbox": BBOX, "modes": results}, fh, indent=2, default=str)
    print(f"\nevidence -> {out}")
    failures = [r for r in results if r["step_state"] != "complete"]
    if failures:
        print(f"FAILED modes: {[r['mode'] for r in failures]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
