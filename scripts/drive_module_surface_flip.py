#!/usr/bin/env python
"""Live driver: every question the module surface flipped, end to end.

Six coarse canaries over one real reach and one real catchment, driven through
the daemon exactly as the plugin drives it. Each is sized so the solve proves the
PLUMBING - the chain, the mesh, the fill, the deck the serializer writes, the
dispatch and the reader - rather than the physics, and each writes the evidence
JSON the proof packet is assembled from.

The discharge is PINNED on the reach runs: a canary that also depended on a live
NWM cycle would report a source outage as a code failure.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_module_surface_flip.py [--only NAME ...] [--timeout 2400] [--out-dir D]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402

#: A real NHDPlus reach WITH NHDArea polygon coverage - the domain is the cut.
LOCATION = "Eel River near Scotia, California"
#: A 2D reach wants ~10 elements across the channel to route flow at all, and the
#: channel is ~150 m wide here. Coarser than this the clean chain takes the
#: ribbon apart: the boundary-face pass sees a one-element-wide strip as slivers
#: and removes every element.
REACH = {
    "location": LOCATION,
    "reach_length_km": 1.0,
    "sim_duration_s": 600.0,
    "spill_duration_s": 120.0,
    "source_q_m3s": 8.0,
    "mesh_resolution_m": 12.0,
    "discharge_m3s": 2.2,
    "input_mode": "auto",
}

#: The Coweeta headwater catchment: a small steep basin with a real outlet, and
#: the one the rain-on-grid front was built and proved on.
CATCHMENT = {
    "location": "Otto, North Carolina",
    "pour_point": [-83.40402, 35.05746],
    # A basin that ALREADY holds water sheds what falls on it, which is what
    # makes this canary's peak depth a picture rather than a flat zero: at
    # AMC-II a forested Coweeta headwater infiltrates a screening storm almost
    # entirely, and a proof whose flagship raster is all zeros discriminates
    # nothing.
    "design_storm_mm_per_hr": 60.0,
    "storm_duration_hr": 1.0,
    "sim_duration_hr": 3.0,
    "antecedent_moisture": "wet",
    # The band the Coweeta basin has been meshed and solved at: coarser than
    # this the channel corridor thins to a strip whose boundary walk leaves a
    # lone liquid node, which the engine's own numbering refuses.
    "mesh_min_edge_m": 25.0,
    "mesh_max_edge_m": 200.0,
    # A catchment's infiltration levers have no data source of their own, so law
    # 9 refuses to run them on a labeled default with nobody to approve it. The
    # door's own review is the surface that approval happens on, which is what
    # this canary also proves.
    "input_mode": "user_gated",
}

CANARIES: dict[str, dict] = {
    "telemac_river_dye": {**REACH, "dye_concentration_mgl": 100.0},
    "telemac_river_oil_spill": {**REACH, "oil_type": "crude",
                                "oil_concentration_mgl": 100.0,
                                "n_drogues": 100, "oil_release_step": 60},
    "telemac_river_scour": {**REACH, "tracer_concentration_mgl": 100.0,
                            "grain_size_um": 200.0},
    # The dredging rule rides ON the same mobile bed, and it is its own canary
    # because NESTOR's surface-reference fence is what a dredged run adds: every
    # field node has to lie between two of its profiles, and consecutive
    # profiles must not cross.
    "telemac_river_scour_dredged": {**REACH, "reach_length_km": 2.0,
                                    "tracer_concentration_mgl": 100.0,
                                    "grain_size_um": 200.0, "dredging": True,
                                    "dredge_volume_m3": 200.0},
    "telemac_river_sediment_plume": {**REACH, "sediment_concentration_mgl": 100.0,
                                     "grain_size_um": 200.0},
    "telemac_do_sag": {"location": LOCATION, "reach_length_km": 1.0,
                       "sim_duration_s": 600.0, "mesh_resolution_m": 12.0,
                       "discharge_m3s": 2.2, "outfall_coords": None,
                       "effluent_bod_mgl": 250.0, "effluent_q_m3s": 1.0,
                       "input_mode": "auto"},
    "telemac_rain_on_grid": CATCHMENT,
}

#: What each canary's answer is READ by - the numbers a reader has to be able to
#: check, off the run's own persisted metrics.
ANSWERS: dict[str, tuple[str, ...]] = {
    "telemac_river_dye": ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m",
                          "active_frames", "mesh_size_m"),
    "telemac_river_oil_spill": ("dye_cmax_mgl", "dye_peak_time_s",
                                "plume_reach_m", "active_frames", "mesh_size_m"),
    "telemac_river_scour": ("max_scour_mm", "max_deposition_mm",
                            "deposited_mass_kg", "deposit_fraction",
                            "active_frames", "mesh_size_m"),
    "telemac_river_scour_dredged": ("max_scour_mm", "max_deposition_mm",
                                    "deposited_mass_kg", "deposit_fraction",
                                    "active_frames", "mesh_size_m"),
    "telemac_river_sediment_plume": ("dye_cmax_mgl", "max_deposition_mm",
                                     "deposited_mass_kg", "deposit_fraction",
                                     "active_frames", "mesh_size_m"),
    "telemac_do_sag": ("do_min_mgl", "do_min_distance_m", "do_violates_standard",
                       "mesh_size_m"),
    "telemac_rain_on_grid": ("peak_discharge_m3s", "runoff_coefficient",
                             "max_depth_peak_m", "continuity_rel_error",
                             "mesh_size_m"),
}

#: A layer's inline GeoJSON above this is bulk, not evidence: the mesh preview
#: alone carries ~2 MB of triangle edges.
_INLINE_GEOJSON_KEEP_BYTES = 4096


def _compact(evidence: dict) -> dict:
    layers = []
    for layer in evidence.get("layers") or []:
        layer = dict(layer)
        blob = json.dumps(layer.get("inline_geojson") or "", default=str)
        if len(blob) > _INLINE_GEOJSON_KEEP_BYTES:
            layer["inline_geojson"] = f"<dropped, {len(blob)} bytes>"
        layers.append(layer)
    return {**evidence, "layers": layers}


def _drive(name: str, timeout: float, out_dir: str) -> dict:
    """One canary. The NAME is the canary's; the TOOL is what it invokes."""
    tool = name.removesuffix("_dredged")
    args = {k: v for k, v in CANARIES[name].items() if v is not None}
    ev = run_live(LiveRun(
        tool=tool, args=args,
        case_title=f"canary: {name} (module surface flip, coarse)",
        answers=GateAnswers(confirm="proceed",
                            require_form=args.get("input_mode") == "user_gated"),
        timeout_s=timeout, cleanup_case=True))
    metrics = ev.metrics or {}
    report = {
        "canary": name,
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
        "detail": ev.detail,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}_coarse_evidence.json")
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
    ap.add_argument("--only", nargs="*", default=None, choices=sorted(CANARIES))
    ap.add_argument("--timeout", type=float, default=2400.0)
    ap.add_argument("--out-dir", dest="out_dir", required=True)
    ns = ap.parse_args()

    failed: list[str] = []
    for tool in (ns.only or list(CANARIES)):
        print(f"\n=== {tool} ===", flush=True)
        try:
            _drive(tool, ns.timeout, ns.out_dir)
        except Exception as exc:  # noqa: BLE001 - the driver reports every canary
            print(f"CANARY FAILED {tool}: {exc}", flush=True)
            failed.append(tool)
    if failed:
        print(f"\nFAILED: {failed}")
        return 1
    print("\nall canaries status=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
