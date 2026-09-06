#!/usr/bin/env python
"""Live driver: the two open-water questions, on the domains they declare.

Three runs, each driven through the daemon exactly as the plugin drives it:

  * ``agitation_om2d`` - the harbour cut from the real shoreline over the AOI,
    the surveyed breakwater punched out of it as a conformal obstacle, the bed
    painted from surveyed topobathy;
  * ``agitation_supplied`` - the SAME question on a mesh built ahead of the run
    and handed to it, which is the arm that proves a caller's own domain is
    adopted rather than rebuilt;
  * ``stratified_om2d`` - a real lake basin, whose boundary names no liquid edge
    at all, which is what a lake IS and what the sheet has to be able to state.

Each is sized so the solve proves the PLUMBING - the chain, the mesh, the fill,
the deck the serializer writes, the dispatch and the reader - rather than the
physics.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_open_water_domains.py --out-dir D [--only NAME ...] [--timeout 3600]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402

#: Point Judith Harbor of Refuge - the AOI the supplied-mesh arm was proven on,
#: a real breakwater across a real approach.
HARBOUR_AOI = (-71.525, 41.338, -71.492, 41.368)
#: Marquette Lower Harbor, Lake Superior - a mapped water body with a surveyed
#: lake-datum bed and no liquid boundary anywhere on its rim.
LAKE_AOI = (-87.39234, 46.52812, -87.36788, 46.55021)

HARBOUR = {
    "bbox": list(HARBOUR_AOI),
    "wave_period_s": 8.0,
    "wave_height_m": 1.0,
    "wave_direction_deg": 160.0,
    "reflection_coef": 0.3,
    "mesh_min_edge_m": 25.0,
    "open_depth_threshold_m": 8.0,
    "input_mode": "auto",
}

BASIN = {
    "bbox": list(LAKE_AOI),
    "warm_temp_c": 18.0,
    "cold_temp_c": 6.0,
    "thermocline_depth_m": 8.0,
    "wind_speed_mps": 6.0,
    "wind_direction_deg": 270.0,
    "levels": 13,
    "mesh_min_edge_m": 60.0,
    "output_interval_min": 10.0,
    "input_mode": "auto",
}

#: What each run's answer is READ by - the numbers a reader has to be able to
#: check, off the run's own persisted metrics.
ANSWERS: dict[str, tuple[str, ...]] = {
    "agitation_om2d": ("kd_max", "hs_max_m", "kd_sheltered", "kd_exposed",
                       "wave_period_s", "mesh_size_m"),
    "agitation_supplied": ("kd_max", "hs_max_m", "kd_sheltered", "kd_exposed",
                           "wave_period_s", "mesh_size_m"),
    "stratified_om2d": ("stratification_dt", "column_heat_drift_frac",
                        "u_surface", "u_bottom", "depth_avg_u", "mesh_size_m"),
}

_INLINE_GEOJSON_KEEP_BYTES = 4096


def breakwater_layer(bbox: tuple[float, ...]) -> str:
    """The surveyed structure this harbour's question is ABOUT -> its layer uri.

    Fetched here rather than declared in the template: which thing shelters is
    the caller's to name, so the template says only what shape it accepts.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    layer = TOOL_REGISTRY["fetch_osm_breakwaters"].fn(bbox=list(bbox))
    return str(layer.uri)


def supplied_harbour_mesh(structure_uri: str, work: Path) -> str:
    """Build the harbour domain AHEAD of the run -> the mesh uri it is handed.

    The recipe is the TEMPLATE's own, read off the declaration rather than
    restated, so the two agitation arms differ in WHO built the mesh and in
    nothing else.
    """
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import recipe_from_plan_value, recipe_plan_value
    from trid3nt_server.workflows.telemac.templates.agitation.agitation import MESH
    from trid3nt_server.workflows.telemac.templates.agitation.barrier import _footprint

    footprint = _footprint(structure_uri,
                           float(HARBOUR.get("barrier_width_m") or 20.0))
    ask = recipe_plan_value(MESH)
    bound = json.loads(json.dumps(ask, default=str))
    bound["extent"] = list(HARBOUR_AOI)
    bound["resolution_m"] = HARBOUR["mesh_min_edge_m"]
    for entry in bound["ops"]:
        if entry["op"] == "set_obstacle":
            entry["kwargs"]["geometry"] = footprint
        if entry["op"] == "enforce_mesh_gradation":
            entry["kwargs"].setdefault("gradation", 0.15)
        if entry["op"] == "identify_ocean_boundary_sections":
            entry["kwargs"]["depth_threshold"] = HARBOUR["open_depth_threshold_m"]
    session = MeshSession(recipe_from_plan_value(bound), case_id=None,
                          name="Point Judith supplied mesh")
    art = session.accept()
    print(json.dumps({"supplied_mesh": art.display_uri, "nodes": art.node_count,
                      "elements": art.element_count}, indent=2))
    return str(art.display_uri)


def _compact(evidence: dict) -> dict:
    layers = []
    for layer in evidence.get("layers") or []:
        layer = dict(layer)
        blob = json.dumps(layer.get("inline_geojson") or "", default=str)
        if len(blob) > _INLINE_GEOJSON_KEEP_BYTES:
            layer["inline_geojson"] = f"<dropped, {len(blob)} bytes>"
        layers.append(layer)
    return {**evidence, "layers": layers}


def _drive(name: str, args: dict[str, Any], tool: str, timeout: float,
           out_dir: str) -> dict:
    ev = run_live(LiveRun(
        tool=tool, args={k: v for k, v in args.items() if v is not None},
        case_title=f"acceptance: {name} (open-water domains)",
        answers=GateAnswers(confirm="proceed",
                            require_form=args.get("input_mode") == "user_gated"),
        timeout_s=timeout, cleanup_case=True))
    metrics = ev.metrics or {}
    report = {
        "run": name,
        "tool": tool,
        "args": args,
        "tool_status": ev.tool_status,
        "step_state": ev.step_state,
        "step_error": ev.step_error,
        "turn_complete": ev.turn_complete,
        "preflight": ev.preflight_note,
        "layers": [layer.get("name") for layer in ev.layers],
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "charts_emitted": ev.charts,
        "answer": {field: metrics.get(field) for field in ANSWERS[name]},
        "boundary_states": metrics.get("boundary_states"),
        "detail": ev.detail,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}_evidence.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"report": report, "evidence": _compact(ev.as_dict())}, fh,
                  indent=2, default=str)
    print(json.dumps(report, indent=2, default=str))
    print(f"evidence -> {os.path.abspath(path)}")
    ev.require_ok()
    ev.require_run_products()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, choices=sorted(ANSWERS))
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--out-dir", dest="out_dir", required=True)
    ns = ap.parse_args()

    work = Path(ns.out_dir) / "work"
    work.mkdir(parents=True, exist_ok=True)
    wanted = ns.only or list(ANSWERS)
    structure = breakwater_layer(HARBOUR_AOI) if any(
        n.startswith("agitation") for n in wanted) else None

    plans: dict[str, tuple[str, dict[str, Any]]] = {}
    if "agitation_om2d" in wanted:
        plans["agitation_om2d"] = ("artemis_harbor_agitation",
                                   {**HARBOUR, "structure": structure})
    if "agitation_supplied" in wanted:
        plans["agitation_supplied"] = (
            "artemis_harbor_agitation",
            {**HARBOUR, "structure": structure,
             "mesh": supplied_harbour_mesh(structure, work)})
    if "stratified_om2d" in wanted:
        plans["stratified_om2d"] = ("telemac3d_stratified_flow", dict(BASIN))

    failed: list[str] = []
    for name, (tool, args) in plans.items():
        print(f"\n=== {name} ===", flush=True)
        try:
            _drive(name, args, tool, ns.timeout, ns.out_dir)
        except Exception as exc:  # noqa: BLE001 - the driver reports every run
            print(f"RUN FAILED {name}: {exc}", flush=True)
            failed.append(name)
    if failed:
        print(f"\nFAILED: {failed}")
        return 1
    print("\nall open-water runs status=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
