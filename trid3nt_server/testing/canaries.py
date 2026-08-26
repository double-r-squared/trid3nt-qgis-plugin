"""The DECLARED canary runs: one named Tier-A invocation per template.

A canary is the path-A test of the three-path model - every unfilled param
supplied on the call, so the gates are SATISFIED rather than skipped - sized so
the solve proves the plumbing and the physics answer in minutes rather than the
half hour a showcase takes. It is a DECLARATION: the tool, the args, the answers
its cards get. Nothing here implements a protocol; :mod:`live_run` does that.

Why this is product code and not a script per template: a canary is what a
migration's REPEATABILITY rests on. The same declaration runs before a change and
after it, and "same question, same answer" is only evidence when both runs came
from one frozen declaration rather than from two hand-typed command lines. One
home, one registry, one runner:

    venvs/agent/bin/python -m trid3nt_server.testing.canaries <name>

writes the run's evidence JSON, which the ``scripts/`` diagnostic lane renders
into the proof sheet (``scripts/render_all_layers_proof.py --evidence ...``).
Proof RENDERING stays out of the product tree by ruling; the declaration does
not.

DEMO VALUES LIVE IN THE DECLARATION. A canary's location, window and station are
here, in a labeled declaration, never as a constant inside workflow code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .live_run import GateAnswers, LiveRun, RunEvidence, run_live
from .proof_paths import evidence_path as _proof_evidence_path

__all__ = ["CANARIES", "evidence_path", "main", "run"]

#: Where a canary's evidence lands: ``docs/proof/templates/<template>/<variant>/``,
#: beside the renders the diagnostic lane writes from it. The FOLDER is
#: ``proof_paths``' to decide - one home, so a render script and the canary that
#: fed it cannot end up in different directories.


# --------------------------------------------------------------------------- #
# TELEMAC family
# --------------------------------------------------------------------------- #

#: Apalachicola Bay, FL - the CO-OPS 8728690 gauge and the Hurricane Michael
#: window (2018-10-09..11). A HISTORICAL window on purpose: a canary that read
#: "the last few days" would report a quiet week as a physics regression.
_COASTAL_BBOX = [-85.02, 29.69, -84.90, 29.80]

#: Lake Superior open water off Marquette - inside the NOAA Great Lakes
#: lake-datum bathymetry coverage, so the REAL-bed path is the one exercised.
_SUPERIOR_BBOX = [-87.60, 46.70, -86.60, 47.20]

#: Marquette Lower Harbor, MI - a REAL harbour with a REAL surveyed breakwater in
#: OpenStreetMap, which is the whole point of the diffraction question class.
_MARQUETTE_BBOX = [-87.392, 46.528, -87.368, 46.550]

#: Coweeta Creek, NC - the gauged headwater catchment the rain-on-grid work was
#: built against. A DOCUMENTED pour point, so the delineation has a real outlet
#: rather than a guessed one.
_COWEETA_POUR_POINT = [-83.40402, 35.05746]

CANARIES: dict[str, LiveRun] = {
    # The Michael surge coast at a coarse grid over a 6-hour window: wide enough
    # that the rising boundary actually floods low land (a canary whose answer is
    # zero cannot detect a regression in the thing it measures), small enough to
    # solve in seconds.
    "coastal_tidal_surge": LiveRun(
        tool="coastal_tidal_surge",
        args={
            "bbox": _COASTAL_BBOX,
            "series_type": "observed",
            "station": "8728690",
            "start_date": "2018-10-09",
            "end_date": "2018-10-11",
            # datum_offset_m is deliberately UNSET: the gauge's own published
            # MLLW -> NAVD 88 offset reconciles the series with the DEM_all bed.
            # Pinning 0.0 here is what cold-started 12 km2 of marsh wet.
            "ocean_edge": "auto",
            "target_resolution_m": 250.0,
            "duration_hours": 6.0,
            "time_step_s": 20.0,
            "bathy_source": "noaa_demall",
            "compute_class": "medium",
            # user_gated, not auto: the resolved window / station / datum go past
            # a review card and the harness answers it. The datum offset is a
            # physics-consequential row, so a run that never showed it to anybody
            # refuses under law 9 - which is the floor working, not a canary to
            # route around.
            "input_mode": "user_gated",
        },
        case_title="canary: coastal tidal surge (Apalachicola Bay, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
    # Lake Superior open water at a coarse grid over the shortest storm the tool
    # accepts. The REAL lake-datum bed, because that is the path a lake question
    # takes; the prescribed storm wind is a labeled demo default and goes past the
    # review card, which is why this runs user_gated too.
    "tomawac_wave_field": LiveRun(
        tool="tomawac_wave_field",
        args={
            "bbox": _SUPERIOR_BBOX,
            "wave_mode": "fetch_growth",
            "wind_speed_mps": 20.0,
            "wind_direction_deg": 270.0,
            "boundary_hs_m": 1.5,
            "boundary_period_s": 10.0,
            "current_speed_mps": -2.5,
            "target_resolution_m": 3000.0,
            "sim_duration_hours": 1.0,
            "bathy_source": "noaa_greatlakes",
            "compute_class": "medium",
            "input_mode": "user_gated",
        },
        case_title="canary: tomawac wave field (Lake Superior, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
    # The sheltering question on a real harbour: the surveyed breakwater comes
    # from OSM and is meshed as a thin solid barrier over the real lake bed, so
    # the sheltered/exposed pair is a measurement rather than a schematic.
    "artemis_harbor_agitation": LiveRun(
        tool="artemis_harbor_agitation",
        args={
            "bbox": _MARQUETTE_BBOX,
            "wave_mode": "diffraction",
            "wave_period_s": 8.0,
            "wave_direction_deg": 129.2,
            "wave_height_m": 2.0,
            "reflection_coef": 0.5,
            "target_resolution_m": 30.0,
            "bathy_source": "noaa_greatlakes",
            "compute_class": "medium",
            "input_mode": "user_gated",
        },
        case_title="canary: artemis harbor agitation (Marquette, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
    # A CALM Lake Superior column: warm over cold, no wind, so the thermocline
    # persists. That is the half of the calm-vs-windy pair whose answer is a
    # NUMBER (the surviving top-to-bottom temperature difference), which is what
    # a parity canary needs.
    "telemac3d_stratified_flow": LiveRun(
        tool="telemac3d_stratified_flow",
        args={
            "bbox": _SUPERIOR_BBOX,
            "flow_mode": "stratification",
            "wind_speed_mps": 0.0,
            "wind_direction_deg": 270.0,
            "warm_temp_c": 25.0,
            "cold_temp_c": 15.0,
            "thermocline_depth_m": 8.0,
            "nplan": 13,
            "target_resolution_m": 3000.0,
            "sim_duration_hours": 1.0,
            "bathy_source": "noaa_greatlakes",
            "compute_class": "medium",
            "input_mode": "user_gated",
        },
        case_title="canary: telemac3d stratified flow (Lake Superior, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
    # THE IDEALIZED PATH, which no other canary walks. Every open-water canary
    # above solves over real geography, so the geography-free analytic domains -
    # the seiche ladder, the Berkhoff shoal, the lock-exchange channel - had no
    # live cover at all, and the one thing they do differently (report NO utm_epsg
    # and rasterize their local metres in a placeholder frame) was the thing that
    # broke. Resonance because its answer is a NUMBER a regression moves: the
    # response at the basin's own resonant period against the response off it.
    # The bbox only places the analytic basin's label on the map; the physics is
    # the harbour geometry, not the location.
    "artemis_harbor_resonance_idealized": LiveRun(
        tool="artemis_harbor_agitation",
        args={
            "bbox": _MARQUETTE_BBOX,
            "wave_mode": "resonance",
            "wave_period_s": 8.0,
            "wave_direction_deg": 90.0,
            "wave_height_m": 1.0,
            # target_resolution_m is deliberately UNSUPPLIED, and this canary is
            # the reason the gap is visible: the param declares bounds of
            # (20, 2000) m while declaring its own analytic-domain default as
            # 8 m, so an EXPLICIT ask is floored to 20 m - and the analytic
            # basin is 100 m wide with a 25 m mouth, which 20 m spacing cannot
            # discretize (the opening lands on 2 columns and the boundary ring
            # gets an isolated liquid point). Letting the labeled 8 m default
            # ride is the declared path and the one that resolves the basin.
            "bathy_source": "idealized",
            "compute_class": "medium",
            "input_mode": "user_gated",
        },
        case_title="canary: artemis harbour resonance (idealized basin, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
    # Coweeta Creek, NC - a small gauged headwater catchment with a documented
    # pour point, so the delineation has a real outlet to snap to. A short
    # design storm: this canary proves the whole delineate -> mesh -> infiltrate
    # -> solve chain rather than studying a flood.
    "telemac_rain_on_grid": LiveRun(
        tool="telemac_rain_on_grid",
        args={
            "pour_point": _COWEETA_POUR_POINT,
            "design_storm_mm_per_hr": 25.0,
            "storm_duration_hr": 1.0,
            "sim_duration_hr": 2.0,
            "antecedent_moisture": "normal",
            "compute_class": "medium",
            "input_mode": "auto",
        },
        case_title="canary: telemac rain on grid (Coweeta Creek, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
}


# --------------------------------------------------------------------------- #
# REFINED-MESH variants: the same question, the resolution lever moved.
# --------------------------------------------------------------------------- #
# NOT parity runs. A different mesh is a different discretization, so the
# scalars MOVE - and that movement is the physics of resolution, information
# about how far the coarse answer can be trusted, never a regression. Each one
# is the coarse declaration with its sizing lever changed and nothing else, so
# the drift has exactly one cause.

def _refined(name: str, **overrides: Any) -> LiveRun:
    base = CANARIES[name]
    return LiveRun(**{**base.__dict__,
                      "args": {**base.args, **overrides},
                      "case_title": base.case_title.replace("canary:", "refined:")
                      .replace("coarse", "refined")})


#: The Eel River reach near Scotia, CA - the cohort's own canary reach, with a
#: real NHDArea-covered channel. The outfall is the USGS Scotia gage (11477000).
_EEL_REACH = "Eel River near Scotia, California"
_EEL_OUTFALL = [-124.0983, 40.4921]

CANARIES.update({
    # THE COHORT'S REFINED RUNS. Their COARSE canaries live in their own drive
    # scripts (scripts/drive_do_sag_cards.py --coarse, drive_river_dye_cards.py
    # --coarse), which is where NATE reviewed them; only the refined variants are
    # declared here, beside the other four.
    #
    # 10 m, and the direction of the width cap is worth stating: the >= 2 cells
    # across the channel rule is a CEILING on coarseness (h <= width / 2 = 30 m
    # on a 60 m channel), not a floor on fineness. The coarse canary asked 100 m
    # and was capped DOWN to 30; asking 10 m stands, and the node budget is the
    # only thing that could raise it.
    "telemac_do_sag_refined": LiveRun(
        tool="telemac_do_sag",
        args={
            "location": _EEL_REACH, "outfall_coords": _EEL_OUTFALL,
            "discharge_bod_mgl": 20.0, "water_temp_c": 20.0,
            "do_standard_mgl": 5.0, "k1_per_day": 0.3, "k2_per_day": 0.9,
            "reach_length_km": 0.5, "sim_duration_s": 600.0,
            "mesh_resolution": "coarse", "mesh_resolution_m": 10.0,
            "discharge_m3s": 60.0,
            # 30 frames over the 600 s window instead of the worker default's 6,
            # for the same reason the dye run asks for them: a sag that develops
            # and settles inside six frames cannot be spot-checked against. The
            # cadence is the SOLVER's output interval, so the run produces more
            # of its own answer rather than the renderer interpolating between
            # fewer.
            "output_interval_min": 0.333,
            "input_mode": "auto",
        },
        case_title="refined: telemac do sag (Eel River near Scotia, 10 m)",
        answers=GateAnswers(confirm="proceed"), cleanup_case=True),
    "telemac_river_dye_refined": LiveRun(
        tool="telemac_river_dye",
        args={
            "location": _EEL_REACH, "substance": "dye",
            "spill_fraction": 0.25, "spill_duration_s": 120.0,
            "dye_concentration_mgl": 100.0, "reach_length_km": 1.0,
            "sim_duration_s": 600.0, "source_q_m3s": 8.0,
            "channel_width_m": 60.0, "mesh_resolution": "coarse",
            "mesh_resolution_m": 10.0, "discharge_m3s": 2.2,
            # 30 frames over the 600 s window instead of the worker default's 6.
            # Six frames is too coarse to spot-check a plume against: the dye
            # arrives and the animation is over. The cadence is the SOLVER's
            # output interval, so this is the run producing more of its own
            # answer rather than the renderer interpolating between fewer.
            "output_interval_min": 0.333,
            "input_mode": "auto",
        },
        case_title="refined: telemac river dye (Eel River near Scotia, 10 m)",
        answers=GateAnswers(confirm="proceed"), cleanup_case=True),
    # 250 -> 50 m. The finer grid should also thin the dot-lattice sparsity in
    # the published raster, since that sparsity is a mesh-vs-output-grid
    # mismatch and this closes the gap by a factor of five.
    "coastal_tidal_surge_refined": _refined("coastal_tidal_surge",
                                            target_resolution_m=50.0),
    # 3000 -> 500 m over a whole-lake fetch; the node budget may coarsen it back
    # and the run says so in its own `coarsened` flag if it does.
    "tomawac_wave_field_refined": _refined("tomawac_wave_field",
                                           target_resolution_m=500.0),
    # 30 -> 15 m. A phase-resolving solve needs nodes per WAVELENGTH, so this is
    # the refinement that matters most for the diffraction fringes.
    "artemis_harbor_agitation_refined": _refined("artemis_harbor_agitation",
                                                 target_resolution_m=15.0),
    # 3000 -> 1000 m at the SAME 13 planes: 3D cost cubes, so the vertical is
    # held fixed and only the horizontal moves.
    "telemac3d_stratified_flow_refined": _refined("telemac3d_stratified_flow",
                                                  target_resolution_m=1000.0),
})


def evidence_path(name: str) -> str:
    return _proof_evidence_path(name)


def run(name: str, *, timeout_s: float | None = None) -> RunEvidence:
    """Drive one declared canary over the live socket, from the top every time.

    ``restart_clean`` is the registry's, not each declaration's, because it is a
    property of what a canary IS. A canary re-run after a code change must
    exercise the code that changed; resume-from-failure would replay the PREVIOUS
    attempt's deck out of the ledger - the same invocation key, the same params -
    and report the old artifact as the new run's answer. That is a correct feature
    doing exactly the wrong thing here, and it is the kind of green that costs an
    afternoon.
    """
    declared = CANARIES.get(name)
    if declared is None:
        raise KeyError(f"no canary named {name!r} (declared: {sorted(CANARIES)})")
    overrides: dict[str, Any] = {"args": {**declared.args, "restart_clean": True}}
    if timeout_s is not None:
        overrides["timeout_s"] = timeout_s
    return run_live(LiveRun(**{**declared.__dict__, **overrides}))


def _answer(ev: RunEvidence) -> dict[str, Any]:
    """The run's own PHYSICAL ANSWER, read off the artifacts it persisted.

    Never recomputed: the metrics document under the run prefix is the product,
    and a parity comparison that rebuilt the number would be comparing two
    implementations rather than two runs.
    """
    return {
        "tool": ev.tool,
        "run_id": ev.run_id,
        "step_state": ev.step_state,
        "step_error": ev.step_error,
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "layers": [layer.get("name") for layer in ev.layers],
        "metrics": ev.metrics,
        "chart_titles": [payload.get("title") for payload in ev.chart_payloads],
        "product_errors": ev.product_errors,
        "detail": ev.detail,
    }


def main(argv: list[str] | None = None) -> int:
    """Drive one canary and exit non-zero unless its PRODUCTS were read.

    ``require_ok`` alone is not the gate. It asks whether the tool dispatched and
    the turn finished, and a run whose object store was not reachable satisfies
    both: the products reader misses, ``metrics`` stays None, and a canary that
    read NOTHING exits 0. That green means the socket worked, which is not what a
    canary is for. ``require_run_products`` is what makes the exit code depend on
    the run's own artifacts, so it is part of the DEFAULT gate - a missing
    TRID3NT_RUNS_BUCKET fails loudly instead of passing quietly.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", choices=sorted(CANARIES))
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=None,
                    help="evidence JSON path (default: the canonical one)")
    ns = ap.parse_args(argv)

    ev = run(ns.name, timeout_s=ns.timeout)
    out = ns.out or evidence_path(ns.name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(ev.as_dict(), fh, indent=2, default=str)
    print(json.dumps({**_answer(ev), "evidence": out}, indent=2, default=str))
    try:
        ev.require_ok().require_run_products()
    except Exception as exc:  # noqa: BLE001 - the reason IS the report
        print(f"CANARY FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
