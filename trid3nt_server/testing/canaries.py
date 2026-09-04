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

writes the run's evidence JSON and, for a DELIVERING variant, assembles the
delivery packet from it - ``scripts/assemble_proof_packet.py``, which renders
every panel, chart and animation the checklist demands, verifies them
mechanically, and writes the ordered ``packet.json`` a reader is handed. Such a
canary that solves but cannot be delivered exits non-zero, because "did we send
the GIF" is not a question anybody should be answering from memory. Proof
RENDERING stays out of the product tree by ruling; the declaration does not.

EVERY VARIANT OWES ITS PACKET. The coarse lane is the SILENT-PIN one - its
evidence is compared run against run to catch drift, and it is not what a reader
is handed - but a pin whose renders nobody assembled is a pin nobody
interrogated, so its packet is assembled and verified exactly like the flagship's.

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
from .proof_animations import PROOF_ANIMATIONS
from .proof_paths import evidence_path as _proof_evidence_path
from .proof_paths import split_variant

__all__ = ["CANARIES", "PROOF_ANIMATIONS", "assemble_packet", "evidence_path",
           "main", "run"]

#: Re-exported so the canary registry and the animation ruling read as one
#: declaration surface: this file says WHAT to run, ``proof_animations`` says
#: WHICH field of the result the delivered animation paints and why. Both are
#: declarations, neither is inferred, and the packet assembler reads both.

#: Where a canary's evidence lands: ``docs/proof/templates/<template>/<variant>/``,
#: beside the renders the diagnostic lane writes from it. The FOLDER is
#: ``proof_paths``' to decide - one home, so a render script and the canary that
#: fed it cannot end up in different directories.


# --------------------------------------------------------------------------- #
# TELEMAC family
# --------------------------------------------------------------------------- #

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

#: The canary's design storm, CITED: the 10-year / 24-hour point precipitation
#: depth at the Coweeta pour point is 6.17 in = 156.7 mm - NOAA Atlas 14 Volume 2
#: Version 3 (Ohio River Basin), Precipitation Frequency Data Server
#: (https://hdsc.nws.noaa.gov/pfds/), grid point 35.0583 N 83.4000 W, read
#: 2026-09-02 through ``lookup_precip_return_period``. A REAL design depth for
#: this catchment rather than a round number a reader cannot look up: the storm
#: the acceptance hydrograph is measured against has to have a source.
_COWEETA_ATLAS14_10YR_24H_MM = 156.7
_COWEETA_STORM_HR = 24.0

#: The OUTER BREAKWATER of Marquette Lower Harbor, as surveyed in OpenStreetMap
#: (``man_made=breakwater``), handed to the structure slot the way a drawn one is:
#: a polyline of (lon, lat) vertices. It runs roughly north-south across the
#: harbour approach with its southern root hooking west to the shore, and the
#: water it exists to protect sits behind it, to the west - as much of that water
#: as the lake-datum bathymetry actually covers. Baked here rather than
#: fetched because a canary is a DECLARATION - the same question every time - and
#: a run whose geometry arrived from a live Overpass query would report an
#: upstream outage as a drift in the answer. The three ways the slot can be
#: filled (a fetched layer, a drawn line, nothing at all) are proved together in
#: scripts/drive_artemis_structure_slot.py, which is where the fetch belongs.
_MARQUETTE_BREAKWATER: list[list[float]] = [
    [-87.37902, 46.54432], [-87.37902, 46.54403], [-87.37904, 46.54362],
    [-87.37905, 46.54240], [-87.37897, 46.53918], [-87.37892, 46.53741],
    [-87.37891, 46.53718], [-87.37902, 46.53708], [-87.37900, 46.53659],
    [-87.37892, 46.53640], [-87.37881, 46.53630], [-87.37481, 46.53349],
    [-87.37466, 46.53360], [-87.37850, 46.53630], [-87.37874, 46.53671],
    [-87.37885, 46.54128], [-87.37888, 46.54368], [-87.37889, 46.54422],
    [-87.37881, 46.54457], [-87.37877, 46.54471], [-87.37889, 46.54476],
    [-87.37902, 46.54435], [-87.37902, 46.54432],
]

CANARIES: dict[str, LiveRun] = {
    # THE QUESTION THE TEMPLATE IS NAMED FOR: does the breakwater shelter the
    # berths. A REAL structure is in the slot, so the domain has something to
    # shelter and the sheltered/exposed pair is a measurement across ONE barrier
    # rather than two halves of an empty AOI. The heading is the one the harbour
    # is OPEN to: the basin's mouth is at its south-east end, past the
    # breakwater's hooked root, so swell arriving from the south-south-east runs
    # up into the berths and that is the wave the structure exists to block. In
    # the trig convention the param declares, propagating toward the
    # north-north-west is 110 deg. The unfilled slot is a DIFFERENT question -
    # what an unsheltered approach does - and lives with the other two ways to
    # fill it in scripts/drive_artemis_structure_slot.py.
    "artemis_harbor_agitation": LiveRun(
        tool="artemis_harbor_agitation",
        args={
            "bbox": _MARQUETTE_BBOX,
            "structure": _MARQUETTE_BREAKWATER,
            "wave_mode": "diffraction",
            "wave_period_s": 8.0,
            "wave_direction_deg": 110.0,
            "wave_height_m": 2.0,
            "reflection_coef": 0.5,
            "target_resolution_m": 30.0,
            "bathy_source": "noaa_greatlakes",
            "compute_class": "medium",
            "input_mode": "user_gated",
        },
        case_title="canary: artemis harbor agitation (Marquette breakwater, coarse)",
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
    # pour point, so the delineation has a real outlet to snap to. The storm is
    # the CITED Atlas 14 10-yr / 24-h depth above, spread as a constant rate over
    # its own duration, watched six hours past the rain so the recession limb is
    # inside the window. AMC II is the BASELINE of the antecedent-moisture pair:
    # a normal-condition catchment under a design storm a reader can look up, and
    # the AMC III run of the same storm is what shows the infiltration machinery
    # responds. The cadence is stated because the default writes every ten
    # minutes, which over a thirty-hour window is a hundred and eighty frames.
    "telemac_rain_on_grid": LiveRun(
        tool="telemac_rain_on_grid",
        args={
            "pour_point": _COWEETA_POUR_POINT,
            "design_storm_mm_per_hr": round(
                _COWEETA_ATLAS14_10YR_24H_MM / _COWEETA_STORM_HR, 2),
            "storm_duration_hr": _COWEETA_STORM_HR,
            "sim_duration_hr": _COWEETA_STORM_HR + 6.0,
            "output_interval_min": 30.0,
            "antecedent_moisture": "normal",
            "compute_class": "medium",
            # user_gated, not auto, for the same reason the four open-water
            # canaries are: the template declares physics-consequential rows with
            # labeled defaults (the infiltration model, the land-cover product,
            # the soil-store trio), and a run that showed them to nobody refuses
            # under law 9. That is the floor working, not a canary to route
            # around, so the harness answers the card instead of turning it off.
            "input_mode": "user_gated",
        },
        case_title="canary: telemac rain on grid (Coweeta Creek, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
}


# --------------------------------------------------------------------------- #
# REFINED variants: the same question, ONE discretization lever moved.
# --------------------------------------------------------------------------- #
# NOT parity runs. A different mesh is a different discretization, so the
# scalars MOVE - and that movement is the physics of resolution, information
# about how far the coarse answer can be trusted, never a regression. Each one
# is the coarse declaration with ONE lever changed and nothing else, so the
# drift has exactly one cause. The lever is usually the mesh; where the coarse
# run's answer is bounded by its TIME window instead, it is the window.

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
    # THE COHORT'S REFINED RUNS. Their SMALL runs live in their own drive scripts
    # (scripts/drive_do_sag_cards.py --smoke, drive_river_dye_cards.py --coarse),
    # which is where NATE reviewed them; only the delivering variants are declared
    # here, beside the other four.
    #
    # THE SAG THAT IS ACTUALLY A SAG. A DO sag is a TRAVEL-TIME answer: the load
    # has to ride far enough down the reach for k1 * t to reach order one, or the
    # oxygen the discharge consumes is unmeasurable and the curve is whatever the
    # boundary imposed. So this run is declared around the physics rather than
    # around a short wall clock - a summer LOW FLOW, which is the condition a
    # permit is written for, a reach long enough to hold both the critical point
    # and the recovery below it, a window several travel times deep so the sag has
    # settled, rates at the shallow-stream end of the documented band, and a
    # coarse-but-sane mesh that keeps the solve inside a coffee break. The sag
    # minimum then sits mid-reach with room to recover before the outflow, which
    # is the shape the closed form predicts and the thing a reader can check.
    "telemac_do_sag_refined": LiveRun(
        tool="telemac_do_sag",
        args={
            "location": _EEL_REACH, "outfall_coords": _EEL_OUTFALL,
            # The OUTFALL: a mid-size municipal discharge, oxygen-poor and
            # CBOD-rich, at LATE-SUMMER LOW FLOW - which is when a permit is
            # decided, because that is when the river has least water to dilute
            # with and least speed to carry the load away before it consumes the
            # oxygen. Eight to one dilution at a 7Q10-scale summer flow, a
            # documented shallow-stream deoxygenation rate, and a reaeration rate
            # three times it.
            "effluent_bod_mgl": 300.0, "effluent_q_m3s": 0.5,
            "effluent_do_mgl": 1.0,
            "water_temp_c": 20.0, "do_standard_mgl": 5.0,
            "k1_per_day": 2.0, "k2_per_day": 6.0,
            # FOUR kilometres and forty-eight hours. A sag is a TRAVEL-TIME
            # answer: the critical point sits hours downstream of the outfall, so
            # the reach has to hold it AND the recovery below it, and the window
            # has to cover the load's whole journey through that reach several
            # times over or what is read is the plume front rather than the
            # settled sag. Four kilometres also keeps the reach close to straight,
            # which matters because the downstream axis the sag curve is binned on
            # is a principal-axis proxy rather than the centreline.
            "reach_length_km": 4.0, "sim_duration_s": 172800.0,
            # About six cells across the channel: coarse enough that two days of
            # simulated time run inside half an hour, fine enough that the reach's
            # two end transects come off the contour as contiguous liquid runs.
            "mesh_resolution_m": 24.0,
            "discharge_m3s": 4.0,
            # 24 frames over the 48 h window: the sag develops over hours, so a
            # two-hourly cadence is what shows it arriving and settling rather
            # than the renderer interpolating between a handful.
            "output_interval_min": 120.0,
            "input_mode": "auto",
        },
        case_title="refined: telemac do sag (Eel River near Scotia, 4 km / 48 h)",
        answers=GateAnswers(confirm="proceed"), cleanup_case=True),
    "telemac_river_dye_refined": LiveRun(
        tool="telemac_river_dye",
        args={
            "location": _EEL_REACH, "substance": "dye",
            "spill_fraction": 0.25, "spill_duration_s": 120.0,
            "dye_concentration_mgl": 100.0, "reach_length_km": 1.0,
            "sim_duration_s": 600.0, "source_q_m3s": 8.0,
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
    # 30 -> 20 m. A phase-resolving solve needs nodes per WAVELENGTH, so this is
    # the refinement that matters most for the diffraction fringes. 20 m is the
    # builder's own floor, which the ResolutionSpec states: an 8 s swell in ten
    # metres of water is about a 78 m wave, so the refined run resolves it on
    # roughly four nodes and the coarse one on two - which is why the coarse
    # variant is a pin and the refined one is what gets delivered.
    "artemis_harbor_agitation_refined": _refined("artemis_harbor_agitation",
                                                 target_resolution_m=20.0),
    # 3000 -> 1000 m at the SAME 13 planes: 3D cost cubes, so the vertical is
    # held fixed and only the horizontal moves.
    "telemac3d_stratified_flow_refined": _refined("telemac3d_stratified_flow",
                                                  target_resolution_m=1000.0),
    # The MESH, as everywhere else here: the coarse canary's window already
    # closes six hours past its storm, so the answer is no longer bounded by how
    # long the run watched. 40 -> 25 m in the channel band is where a peak depth
    # and a crest magnitude live.
    "telemac_rain_on_grid_refined": _refined("telemac_rain_on_grid",
                                             mesh_min_edge_m=25.0),
})


def evidence_path(name: str) -> str:
    return _proof_evidence_path(name)


def run(name: str, *, timeout_s: float | None = None) -> RunEvidence:
    """Drive one declared canary over the live socket, from the top every time.

    From the top is the DRIVER's default (``live_run.drive``), so a declaration
    here states the question and nothing about resumption.
    """
    declared = CANARIES.get(name)
    if declared is None:
        raise KeyError(f"no canary named {name!r} (declared: {sorted(CANARIES)})")
    overrides: dict[str, Any] = {}
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


def assemble_packet(name: str, out_dir: str | None = None,
                    evidence: str | None = None) -> dict:
    """The canary's DELIVERY PACKET - the checklist, assembled and verified.

    A canary that finished is not a canary that can be handed to anybody: the
    panels, the canvas view, the charts, the animation and the evidence JSON are
    the deliverable, and "did we send the GIF" was a remembered question until
    this. ``scripts/assemble_proof_packet.py`` answers it mechanically and writes
    ``packet.json`` beside the renders, so every canary close either produces the
    ordered list of what to send or fails loudly saying what is missing.

    ``out_dir`` names where those renders land and ``evidence`` names the JSON
    they are assembled FROM. Both unset, that is the template's own proof folder;
    named, the checklist is assembled somewhere the frozen proof tree is not
    written to, off the run that was just driven, which is how an acceptance
    drive owes a full packet without editing delivered evidence.

    Imported BY PATH because proof RENDERING stays out of the product tree by
    ruling - the declaration lives here, the renderers do not.
    """
    import importlib.util

    template, variant = split_variant(name)
    script = (os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))) + "/scripts/assemble_proof_packet.py")
    spec = importlib.util.spec_from_file_location("assemble_proof_packet", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("assemble_proof_packet", module)
    spec.loader.exec_module(module)
    return module.assemble(template, variant, out_dir=out_dir, evidence=evidence)


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
    ap.add_argument("--packet", dest="packet", action="store_true", default=True,
                    help="assemble + verify the delivery packet (default on)")
    ap.add_argument("--no-packet", dest="packet", action="store_false")
    ap.add_argument("--packet-dir", default=None,
                    help="assemble the packet HERE rather than in the template's "
                         "proof folder - the lane that owes the whole checklist "
                         "without writing into the frozen proof tree")
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
    if not ns.packet or (ns.out and not ns.packet_dir):
        # An evidence file written somewhere other than the canonical proof path
        # has no variant folder to assemble into, so the packet step is skipped
        # rather than pointed at a directory it does not own.
        return 0
    try:
        packet = assemble_packet(ns.name, out_dir=ns.packet_dir,
                                 evidence=ns.out if ns.packet_dir else None)
    except Exception as exc:  # noqa: BLE001 - the reason IS the report
        print(f"PACKET FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"packet": f"{packet['directory']}/packet.json",
                      "verdict": packet["verdict"],
                      "deliverables": len(packet["deliverables"]),
                      "missing": packet["missing"]}, indent=2))
    if packet["verdict"] != "PASS":
        print(f"PACKET REFUSED - {len(packet['missing'])} gap(s); the run is green "
              "and its proof is not deliverable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
