"""THE OPENING: the state the run starts from, and the water level it starts at.

An open-water domain opens at the level the question states or the sea it stands
in; a reach carrying a discharge opens at the normal depth that flow holds over
the channel its inflow face cuts. A run that carries on from another opens at
that run's own last surface. Nothing is painted where nothing was measured - a
domain with no level and no flow refuses by name."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from trid3nt_server.workflows.runtime import journal_note
from trid3nt_server.workflows.runtime.journal import run_choices
from ..errors import TelemacError
from ..helpers.time_step import MESH_H_FLOOR_M, suggest_time_step_s
from ..helpers.uniform_flow import normal_depth_stage
from .accepted_mesh import (
    boundary_uri,
    face_section,
    geometry_uri,
    mesh_artifact,
    mesh_facts,
    mesh_missing,
    mesh_nodes,
    refuse_a_sealed_domain,
    slug_of,
    topology_of,
)

__all__ = ["BED_PARALLEL", "FLAT", "initial_state_of", "open_channel",
           "open_water"]

#: What a continued run's PREVIOUS COMPUTATION FILE is called in the run
#: directory. The engine reads a file, not a URI, so the previous run's restart
#: record is staged under one name and the steering file names that.
_PREVIOUS_DEST = "previous.slf"


#: The engine's perfect-restart record, under the name a body that keeps one
#: writes it.
_RESTART = "restart_domain.slf"


#: How a restart record names its depth, and the depth a node has to hold at
#: that instant to count as water a source can be released into.
_DEPTH_VARIABLE = ("WATER DEPTH", "HAUTEUR D'EAU", "HAUTEUR D EAU")


_WET_DEPTH_M = 0.01


#: What the engine is told to lay: a flat surface at the level, or a sheet of one
#: depth following the bed. Flat is what any body of water does; bed-parallel is
#: what a reach that FALLS holds, and only its own addition measures that.
FLAT = "CONSTANT ELEVATION"


BED_PARALLEL = "CONSTANT DEPTH"


#: The three keys every deck reads the water off, and the two an addition fills
#: where the edge carries values. A body nobody stated a level for carries None
#: in all of them: no water was measured, and nothing is claimed.
_NO_WATER: dict[str, Any] = {
    "level_m": None, "depth_m": None, "max_depth_m": None, "opening": None,
    "inflow_q_m3s": None, "outflow_stage_m": None,
    # THE WINDOW behind each of the two numbers above, where the record that
    # reported them reported one: what the boundary's own file is written from.
    "inflow_q_series": None, "outflow_stage_series": None,
}


#: The class a level slot asks for. A run that asked for one and got nothing is
#: the only run this refuses on: a question declaring no level slot opens the way
#: its own deck says.
_LEVEL_CLASS = "water level series"


def _reach_section_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_MESH_SECTION_UNMEASURED")


def _domain_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_DOMAIN_UNMEASURED")


def _refuse_dry(where: str) -> None:
    """A CLOSED BODY THAT OPENED AT NOTHING: no edge feeds it, its bed is on a
    datum, and the level slot the question asked for came back empty, so the
    surface it stands at is unknown and a solve would run a dry basin and
    report it as an answer. The refusal names that slot and lists what was
    asked for it."""
    asked = [choice for choice in run_choices()
             if choice.need == _LEVEL_CLASS and not choice.picked]
    if not asked:
        return
    slot, rows = asked[0].slot, asked[0].rows
    listed = ", ".join(row.fetcher for row in rows) or "no source at all"
    raise TelemacError(
        f"{where} has no edge water enters by and a bed on a datum, and its "
        f"{slot!r} slot came back empty: {listed} answered nothing for a water "
        "level series over this domain, so the surface this body stands at is "
        f"unknown. State the level on {slot!r}, or supply a bed measured as a "
        "depth below the free surface.",
        error_code="TELEMAC_LEVEL_UNMEASURED")


def _continuation_state(uri: str) -> dict[str, Any]:
    """The restart record's own last instant and depth field - read off the file.

    A continued run is the same declared scenario over an extended horizon."""
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.modules.outputs import read_selafin

    with tempfile.TemporaryDirectory(prefix="telemac-continue-") as tmp:
        path = Path(tmp) / _PREVIOUS_DEST
        _download_object(str(uri), path)
        record = read_selafin(path)
    if len(record["times"]) == 0:
        raise TelemacError(
            f"{uri} holds no time record, so there is no state to continue from "
            "and no instant to continue the scenario at. Point continue_from at "
            f"a completed run's {_RESTART}.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    depth = next((record["data"][name] for name in record["varnames"]
                  if name.strip().upper() in _DEPTH_VARIABLE), None)
    if depth is None:
        raise TelemacError(
            f"{uri} carries no water depth among {record['varnames']}, so the "
            "state this run would start from cannot say where it is wet.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    # Only the file can say where the continued leg stopped: the engine writes the
    # restart at its own last time step, which is not the graphic period, not the
    # asked duration, and not anything the server can compute from the ask. The
    # depth at that instant is the initial state, which decides where a release
    # can land.
    start_s = float(record["times"][-1])
    wet = np.asarray(depth[-1], dtype=float) > _WET_DEPTH_M
    return {
        "start_s": start_s, "wet": wet,
        "note": (f"the restart record this run continues from, at t={start_s:.0f} s: "
                 f"{int(wet.sum())} of {record['npoin']} nodes clear the "
                 f"{_WET_DEPTH_M} m wet floor"),
    }


def initial_state_of(continue_from: str | None, node_count: int) -> dict[str, Any]:
    """WHAT THE RUN STARTS FROM: a fresh reach opens at the derived normal depth
    laid bed-parallel, a positive depth at every node; a CONTINUED one opens at
    the restart record's own wet/dry field and the instant it stands at."""
    if continue_from:
        return _continuation_state(str(continue_from))
    return {"start_s": None, "wet": [True] * node_count,
            "note": "the deck's own constant initial depth, the derived normal "
                    "depth laid bed-parallel over every node of the accepted mesh"}


def _liquid_boundaries(topology: Mapping[str, Any], node_xy: Any
                       ) -> list[dict[str, Any]]:
    """Each numbered liquid boundary, where it sits: the centroid of its role's
    nodes in the mesh's own metres. A role landing as several sections shares
    one centroid, because the topology records nodes per role."""
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    out = []
    for number, role in enumerate(topology["liquid_boundary_order"], start=1):
        nodes = [int(n) for n in (topology["roles"].get(role) or ())]
        out.append({"number": number, "role": str(role),
                    "x": round(float(xy[nodes, 0].mean()), 3),
                    "y": round(float(xy[nodes, 1].mean()), 3)})
    return out


def _seconds(clock: Any) -> float:
    """How long the water is open for, off however its module spells the length.

    A window stated as one number, or the keyword values whose PRODUCT is it - a
    step and a count of them. A module that does not march in time spells none
    and its water opens for the single record a steady solve writes."""
    if isinstance(clock, (list, tuple)):
        return math.prod(float(value) for value in clock) if clock else 0.0
    return float(clock or 0.0)


def _window(reading: Any) -> Any:
    """The series an ingested reading carried, or ``None``.

    A number stated on the call carries none: one number is the whole of what
    the caller said, and the boundary states it as the constant the engine
    reads."""
    return getattr(reading, "series", None)


def _held_level(stage: Any) -> tuple[float, str] | None:
    """The water-surface ELEVATION somebody measured at the outflow, or ``None``.

    An elevation on the datum the bed is painted on - never a height above a
    gauge's own zero, which is a different number about a different surface."""
    from trid3nt_server.inputs.observation import Observation

    if stage is None:
        return None
    if isinstance(stage, (int, float)) and not isinstance(stage, bool):
        return float(stage), "the outflow level was handed over as a number."
    if not isinstance(stage, Observation):
        raise TelemacError(
            f"the outflow level for this run arrived as {type(stage).__name__}, "
            "which is a record rather than a reading: which site reports the "
            "level and how old the sample is are the observation slot's to "
            "decide. Name the row level so one value reaches this step.",
            error_code="TELEMAC_STAGE_UNINGESTED")
    return float(stage.value), (
        f"the outflow holds at {stage.value:g} m, which "
        f"{stage.site_name or stage.site_id or 'the record'} reported"
        + (f" on {stage.sampled}" if stage.sampled else "") + ".")


def _stands_over_the_reach(bed: Mapping[str, Any], level_m: float) -> bool:
    """Is this measured level the water over THIS reach, rather than part of it?

    Over the inflow run's own bed, yes: the surface covers the reach end to end.
    Below the outflow section altogether, also yes - it is not a level on this
    bed at all and the opening refuses by name rather than quietly deriving one
    instead. In between is the reach a uniform-flow depth is for: a horizontal
    surface at the outflow's level would leave the inflow face dry."""
    section = [float(z) for _offset, z in (bed.get("outflow_section") or ())]
    return (level_m > float(bed["bed_top_m"])
            or (bool(section) and level_m <= min(section)))


def _level_opening(bed: Mapping[str, Any], held: tuple[float, str], *,
                   discharge_q: float) -> dict[str, Any]:
    """The opening a reach that does not FALL takes: the level that was measured.

    A uniform-flow depth is a fall over a length, so measured ends that sit level
    have none; the water still stands where the measurement says it does, and the
    run opens flat at the depth that leaves over the outflow section."""
    stage_m, note = held
    section = [(float(offset), float(z))
               for offset, z in (bed.get("outflow_section") or ())]
    if len(section) < 2:
        raise TelemacError(
            f"the outflow run's own section carries {len(section)} point(s), so "
            "there is no bed under the level the outflow is held at.",
            error_code="TELEMAC_OUTFLOW_SECTION_UNMEASURED")
    thalweg = min(z for _offset, z in section)
    depth = stage_m - thalweg
    if depth <= 0.0:
        raise TelemacError(
            f"the outflow is held at {stage_m:.3f} m and the deepest node of the "
            f"outflow run's own section sits at {thalweg:.3f} m, so the level "
            "handed over is at or below the bed. A level here is an ELEVATION on "
            "the datum the bed is painted on, not a height above a gauge's zero.",
            error_code="TELEMAC_OUTFLOW_STAGE_BELOW_BED")
    return {"stage_m": stage_m, "depth_m": depth, "slope": 0.0,
            "drop_m": float(bed["bed_drop_m"]),
            "length_m": float(bed["reach_length_m"]),
            "held": note, "q_m3s": float(discharge_q)}


def _reported_discharge(carrier: Any) -> tuple[float, str]:
    """The streamflow the carrier OBSERVATION reports, with what reported it.

    One value, whichever way it arrived: a stated number and a record read the
    same. A record that never passed its slot's ingestion is refused by name
    rather than read here: choosing the nearest site is what the observation
    slot does."""
    from trid3nt_server.inputs.observation import Observation

    if isinstance(carrier, (int, float)) and not isinstance(carrier, bool):
        return float(carrier), (
            f"the discharge {float(carrier):g} m3/s was stated on the call.")
    if not isinstance(carrier, Observation):
        raise TelemacError(
            f"the carrier for this run arrived as {type(carrier).__name__}, which "
            "is a record rather than a reading: which site reports the flow and "
            "how old the sample is are the observation slot's to decide. Name "
            "the row discharge so one value reaches this step.",
            error_code="TELEMAC_CARRIER_UNINGESTED")
    where = carrier.site_name or carrier.site_id
    if not where and not carrier.sampled:
        # A reading with no site and no moment is the value the caller STATED,
        # which the slot carries in the same shape as a record so this step reads
        # one thing; saying a record reported it would name a source nobody read.
        return float(carrier.value), (
            f"the discharge {carrier.value:g} m3/s was stated on the call.")
    return float(carrier.value), (
        f"the discharge {carrier.value:g} m3/s is what "
        f"{where or 'the carrier record'} reported"
        + (f" on {carrier.sampled}" if carrier.sampled else "") + ".")


def _measured_channel(roles: Mapping[str, Any], node_xy: Any,
                      node_bed: Any) -> dict[str, Any]:
    """What the accepted mesh says about the channel the outflow stage rests on.

    The two bed medians are over the nodes the inflow and outflow runs name, and
    the fall between them over the distance between them IS the friction slope -
    measured on the mesh rather than on a line laid beside it."""
    import numpy as np

    bed = None if node_bed is None else np.asarray(node_bed, dtype=float)
    xy = None if node_xy is None else np.asarray(node_xy, dtype=float)
    medians: dict[str, float] = {}
    centres: dict[str, Any] = {}
    role_nodes: dict[str, list[int]] = {}
    for role in ("inflow", "outflow"):
        nodes = [int(n) for n in ((roles or {}).get(role) or ())]
        if bed is None or max(nodes) >= bed.shape[0]:
            raise TelemacError(
                f"the outflow stage is derived over the painted bed at the "
                f"{role!r} run's own nodes, and the accepted mesh carries "
                f"{0 if bed is None else bed.shape[0]} bed values under roles "
                f"{sorted(roles or {})}.",
                error_code="TELEMAC_MESH_BED_UNMEASURED")
        median = float(np.nanmedian(bed[nodes]))
        if not np.isfinite(median):
            raise TelemacError(
                f"every node the {role!r} run names carries an unpainted bed, so "
                "the outflow stage has no ground to be measured from.",
                error_code="TELEMAC_MESH_BED_UNMEASURED")
        medians[role] = median
        centres[role] = xy[nodes].mean(axis=0)
        role_nodes[role] = nodes
    length = float(np.hypot(*(centres["outflow"] - centres["inflow"])))
    return {"bed_top_m": medians["inflow"],
            "bed_drop_m": medians["inflow"] - medians["outflow"],
            "reach_length_m": round(length, 3),
            "outflow_section": face_section(role_nodes["outflow"], node_xy, bed,
                                             missing=_reach_section_unmeasured)}


def _opening(level: Any, mesh: Mapping[str, Any]) -> dict[str, Any]:
    """THE OPENING: the level this domain stands at and the depth that leaves.

    ``level`` is what somebody measured: a number, an observation, or the record
    an ADDITION returned after measuring the water over this same mesh - a reach
    holds its own opening, and the base takes it rather than deriving a second
    one. Nothing measured and a bed stated as a DEPTH is the bed's own zero;
    nothing measured over a bed on a datum is no water, which is an answer and
    not a refusal - a deck that needs none opens the way it says."""
    import numpy as np

    # A bed STATED as a depth is counted from the free surface itself, so the
    # mesh knows where that surface stands and nothing has to be read for it.
    zero = dict(mesh.get("provenance") or {}).get("free_surface_m")
    if isinstance(level, Mapping):
        measured = {**_NO_WATER, **{k: v for k, v in level.items() if k in _NO_WATER}}
    else:
        measured = dict(_NO_WATER)
        measured["level_m"] = (None if level is None
                               else float(getattr(level, "value", level)))
    if measured["level_m"] is None and zero is None:
        return dict(_NO_WATER)
    surface = float(measured["level_m"] if measured["level_m"] is not None
                    else zero)
    _points, node_bed = mesh_nodes(mesh)
    bed = np.asarray(node_bed, dtype=float)
    floor, ceiling = float(np.nanmin(bed)), float(np.nanmax(bed))
    measured["level_m"] = round(surface, 3)
    if not isinstance(level, Mapping):
        # WHAT AN OPEN EDGE HOLDS on a body with no channel to derive a stage:
        # the level somebody measured, and the reading's own window where it
        # carried one - a tide is a level that moves, and a boundary read off
        # one number would hold the sea still.
        measured["outflow_stage_m"] = measured["level_m"]
        measured["outflow_stage_series"] = _window(level)
    measured["opening"] = measured["opening"] or FLAT
    if measured["opening"] == BED_PARALLEL:
        # A SHEET OF ONE DEPTH following the bed: every node holds that depth,
        # and the column is it. The level is where the outflow section stands
        # under that sheet, which is the only place the two agree.
        depth = float(measured["depth_m"])
        measured["max_depth_m"] = round(depth, 2)
        journal_note(
            f"the opening: bed-parallel at {depth:.3f} m over a bed the "
            f"accepted mesh carries between {floor:.3f} m and {ceiling:.3f} m, "
            f"so every node holds that depth and the outflow section stands at "
            f"{measured['level_m']:.3f} m.")
        return measured
    deepest = surface - floor
    if deepest <= 0.0:
        raise TelemacError(
            f"the free surface opens at {surface:.3f} m and the deepest node of "
            "the accepted mesh sits at or above it, so this domain holds no "
            "water to solve in. State the depth the body holds, or name a bed "
            "measured on the datum the level is read on.",
            error_code="TELEMAC_DOMAIN_HOLDS_NO_WATER")
    wet = surface - bed
    measured["max_depth_m"] = round(deepest, 2)
    if measured["depth_m"] is None:
        measured["depth_m"] = round(float(np.nanmean(wet[wet > 0.0])), 3)
    journal_note(
        f"the opening: FLAT at {measured['level_m']:.3f} m over a "
        f"bed the accepted mesh carries between {floor:.3f} m and "
        f"{ceiling:.3f} m, so the deepest column is "
        f"{measured['max_depth_m']:.2f} m and "
        f"{float((wet > 0.0).mean()) * 100.0:.1f}% of the nodes hold water.")
    return measured


async def open_water(
    *,
    mesh: dict[str, Any],
    duration_s: float | Sequence[float] | None = None,
    level: Any = None,
    name: str = "",
    geometry: str = "geometry.slf",
    boundary: str = "boundary.cli",
    result: str = "results.slf",
    mesh_resolution_m: float | None = None,
    continue_from: str | None = None,
    deck: str = "",
    open_depth_threshold_m: float | None = None,
) -> dict[str, Any]:
    """A BODY OF WATER OPENS at a level over its bed: the mesh it was handed, the
    clock the run turns on, the files the box is given, and THE OPENING - the
    level the surface stands at and the depth that leaves over every node.

    Any body of water: the level is stated or somebody measured it, the
    depth is that level minus the bed, the edge is wall all round and the surface
    opens flat. Nothing here knows a reach from a lake. What a question ADDS on
    top - an inflow's normal depth, an outlet rating curve - is its own step,
    and what that step measured arrives here as the level this one opens at."""
    facts = mesh_facts(mesh, missing=_domain_unmeasured)
    measured = mesh.get("min_edge_m")
    mesh_size_m = round(max(float(measured if measured is not None
                                  else mesh_resolution_m or 0.0),
                            MESH_H_FLOOR_M), 3)
    mesh_resolution_label = (
        f"{mesh_size_m:.3g} m measured minimum edge over "
        f"{facts['mesh_element_count']} elements" if measured is not None
        else f"{mesh_size_m:.3g} m asked edge (mesh unmeasured)")
    time_step_s = suggest_time_step_s(mesh_size_m, mesh=mesh_artifact(mesh))
    node_xy, _bed = await asyncio.to_thread(mesh_nodes, mesh)
    initial_state = await asyncio.to_thread(initial_state_of, continue_from,
                                            len(node_xy))
    topology = await asyncio.to_thread(topology_of, mesh, missing=mesh_missing)
    # A deck that STATES the depth an open edge is designated at is one whose
    # sea state is prescribed across that edge; a deck that states none names a
    # body of water that may legitimately be closed.
    if open_depth_threshold_m is not None:
        refuse_a_sealed_domain(topology, deck=deck or facts["mesh_name"],
                               open_depth_threshold_m=open_depth_threshold_m)
    start_time_s = float(initial_state["start_s"] or 0.0)
    duration_s = _seconds(duration_s)
    slug = slug_of(name or facts["mesh_name"])
    opening = await asyncio.to_thread(_opening, level, mesh)
    if opening["level_m"] is None and not topology["liquid_boundary_order"]:
        _refuse_dry(facts["mesh_name"])
    return {
        "name": slug,
        "title": f"{slug} DOMAIN",
        "location_name": name or facts["mesh_name"],
        "utm_epsg": facts["utm_epsg"],
        "mesh_id": facts["mesh_id"],
        "mesh_size_m": mesh_size_m,
        "mesh_resolution_label": mesh_resolution_label,
        "mesh_resolution_asked_m": mesh_resolution_m,
        "time_step_s": time_step_s,
        "duration_s": duration_s,
        "start_time_s": start_time_s,
        "until_s": start_time_s + duration_s,
        "initial_state": initial_state["note"],
        # THE OPENING, as every deck reads it: what the surface opens at, the
        # depth under it and the keyword that says which of the two the engine is
        # to lay. A run nobody stated a level for carries None for all three and
        # opens the way its own deck says.
        **opening,
        # WHICH dataset painted the mesh's nodes, and where each one stopped: a
        # two-source bed says how much of the domain the survey covered.
        "bed_source": facts["bed_source"],
        "liquid_boundary_order": list(topology["liquid_boundary_order"]),
        "liquid_boundary_prescribes": list(topology["liquid_boundary_prescribes"]),
        # WHERE each numbered liquid boundary sits, so a printed flux can be read
        # at the one a Point stands on.
        "liquid_boundaries": _liquid_boundaries(topology, node_xy),
        "continue_from": _PREVIOUS_DEST if continue_from else None,
        "mesh_inputs": [
            {"gs_uri": geometry_uri(mesh, missing=mesh_missing),
             "dest": geometry},
            {"gs_uri": boundary_uri(mesh, missing=mesh_missing),
             "dest": boundary},
            *([{"gs_uri": str(continue_from), "dest": _PREVIOUS_DEST}]
              if continue_from else [])],
        "server_facts": {
            "utm_epsg": facts["utm_epsg"],
            "bbox": [round(float(v), 6) for v in facts["lonlat_bounds"]],
            "npoin": facts["mesh_node_count"],
            "nelem": facts["mesh_element_count"],
            "mesh_size_m": mesh_size_m,
            "name": slug,
            "duration_s": duration_s,
            "time_step_s": time_step_s,
            "result_slf": result,
            "bed_source": facts["bed_source"]},
    }


async def open_channel(
    *,
    mesh: dict[str, Any],
    friction_law: int,
    friction_coefficient: float,
    carrier: Any = None,
    stage: Any = None,
) -> Any:
    """A CHANNEL OPENS UNDER A DISCHARGE, on top of any body of water: what the
    inflow carries, what the outflow holds, and the depth the run opens at.

    The flow is what the inflow run carries - a reading, or the number stated on
    the call. Where a LEVEL was measured and it stands over the inflow run's own
    bed, that level is what the outflow holds and what the run opens flat at.
    Where none was, the stage is a NORMAL DEPTH over the section the outflow run
    cuts, derived at the roughness the deck is written at, and the run opens
    bed-parallel at that same depth - the equilibrium its own downstream boundary
    holds it to rather than a blanket depth draining into it.

    A run that carries NO CARRIER opens no channel: there is no flow to impose,
    so this hands back the level it was given and the body opens on its level
    boundaries, which is the base's own opening. An edge that names no runs is a
    closed body and is the same nothing."""
    if carrier is None:
        return stage
    node_xy, node_bed = await asyncio.to_thread(mesh_nodes, mesh)
    topology = await asyncio.to_thread(topology_of, mesh, missing=mesh_missing)
    roles = topology["roles"] or {}
    held = _held_level(stage)
    if not (roles.get("inflow") and roles.get("outflow")):
        return {"level_m": None if held is None else round(held[0], 3),
                "depth_m": None, "opening": None,
                "inflow_q_m3s": None, "outflow_stage_m": None,
                "inflow_q_series": None, "outflow_stage_series": None,
                "discharge_note": "this domain's edge names no runs, so no flow "
                                  "is imposed and no stage is derived."}
    law, coefficient = int(friction_law), float(friction_coefficient)
    inflow_q, discharge_note = _reported_discharge(carrier)
    bed = _measured_channel(roles, node_xy, node_bed)
    # A LEVEL SOMEBODY MEASURED is where the water stands, and the run opens flat
    # at it: a uniform-flow depth is a model of the same surface, and a model
    # does not stand over a measurement of the thing it models. The derivation is
    # for the reach that has no measurement - or the one a measurement does not
    # reach, where a horizontal surface at the outflow's level leaves the inflow
    # face dry and the engine has no water to impose a discharge on.
    if held is not None and _stands_over_the_reach(bed, held[0]):
        normal = _level_opening(bed, held, discharge_q=inflow_q)
        journal_note(
            f"the open channel: the outflow HOLDS at the measured level "
            f"{normal['stage_m']:.3f} m and the run opens FLAT at it, which "
            f"stands {normal['stage_m'] - float(bed['bed_top_m']):.3f} m over "
            f"the inflow run's own bed; the measured ends fall "
            f"{normal['drop_m']:.3f} m over {normal['length_m']:.1f} m and no "
            f"uniform-flow depth is derived. {normal['held']} {discharge_note}")
    else:
        normal = normal_depth_stage(bed, law=law, coefficient=coefficient,
                                    discharge_q=inflow_q)
        journal_note(
            f"the open channel: constant depth {normal['depth_m']:.3f} m - the SAME "
            f"normal depth the outflow stage {normal['stage_m']:.3f} m is derived "
            f"as ({normal['q_m3s']:g} m3/s over the measured outflow section at "
            f"{normal['law']} {normal['coefficient']:g}). Bed-parallel at the "
            f"friction slope {normal['slope']:.6f}, which IS the uniform-flow "
            f"surface. {discharge_note}")
    return {
        # The level the base opens the water at, and HOW: a reach that falls
        # holds a sheet of one depth on its own friction slope, and one that
        # does not holds flat at the level somebody measured.
        "level_m": round(float(normal["stage_m"]), 3),
        "depth_m": round(float(normal["depth_m"]), 3),
        "opening": FLAT if float(normal["slope"]) == 0.0 else BED_PARALLEL,
        "outflow_stage_m": round(float(normal["stage_m"]), 3),
        "inflow_q_m3s": inflow_q,
        # The window each number came out of, where the record carried one. The
        # boundary writes the series and the number stands for the boundaries
        # nothing measured a window at.
        "inflow_q_series": _window(carrier),
        "outflow_stage_series": _window(stage),
        "friction_law": law,
        "friction_coefficient": coefficient,
        "discharge_note": discharge_note,
        "normal": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in normal.items()},
    }
