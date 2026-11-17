"""The opening: the state the run starts from, and the water level it starts at.

An open-water domain opens at the stated level or the sea it stands in; a reach carrying a
discharge at the normal depth that flow holds over its inflow face; a continued run at the
previous surface. A domain with no level and no flow refuses by name.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Mapping, Sequence

from trid3nt_server.inputs.run_notes import journal_note
from trid3nt_server.workflows.runtime.journal import run_choices
from ..errors import TelemacError
from ..helpers.time_step import MESH_H_FLOOR_M, suggest_time_step_s
from ..helpers.uniform_flow import normal_depth_stage
from .initial_state import PREVIOUS_DEST, initial_state_of
from .accepted_mesh import (
    boundary_uri,
    face_section,
    geometry_uri,
    mesh_artifact,
    mesh_facts,
    mesh_missing,
    mesh_nodes,
    mesh_zone,
    refuse_a_sealed_domain,
    slug_of,
    topology_of,
)

__all__ = ["FLAT", "SLOPED", "SURFACE_ROUTINE", "open_channel", "open_water", "surface_at",
           "surface_routine"]

# What the engine is told to lay: a flat surface at the level. A sheet of one depth over a measured bed follows every bar and
# pool of it, so a channel opens flat at its outflow level too and the inflow fills the reach to its uniform-flow surface.
FLAT = "CONSTANT ELEVATION"
# A reach held at a measured outflow level below the level its inflow derives opens on the plane between the two, laid
# along the walked line; no keyword lays it, so the engine's own initial-condition routine does (USER_CONDIN_H).
SLOPED = "SPECIAL"
SURFACE_ROUTINE = "user_condin_h.f"


# The three keys every template reads the water off, and the two an addition fills; a body with no stated level carries None in all of them.
_NO_WATER: dict[str, Any] = {
    "level_m": None, "depth_m": None, "max_depth_m": None, "opening": None,
    "inflow_q_m3s": None, "outflow_stage_m": None,
    # The window behind each number, where the record reported one; the boundary file is written from it.
    "inflow_q_series": None, "outflow_stage_series": None,
    # The sloped opening's plane along the walked line; None for a flat one.
    "surface": None,
}


# The class a level slot asks for; only a run that asked for one and got nothing refuses.
_LEVEL_CLASS = "water level series"


def _reach_section_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_MESH_SECTION_UNMEASURED")


def _domain_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_DOMAIN_UNMEASURED")


def _refuse_dry(where: str) -> None:
    """A closed body that opened at nothing (no edge, datum bed, empty level slot) would solve a dry basin and publish it; refuses naming the slot."""
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


def _liquid_boundaries(topology: Mapping[str, Any], node_xy: Any
                       ) -> list[dict[str, Any]]:
    """Each numbered liquid boundary's centroid in the mesh's metres; a role landing as several sections shares one centroid."""
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
    """How long the water is open for: one number, or the product of a step and a count.

    A module that does not march in time opens for the single record a steady solve writes.
    """
    if isinstance(clock, (list, tuple)):
        return math.prod(float(value) for value in clock) if clock else 0.0
    return float(clock or 0.0)


def _window(reading: Any) -> Any:
    """The series an ingested reading carried, or ``None`` for a number stated on the call."""
    return getattr(reading, "series", None)


def _held_level(stage: Any) -> tuple[float, str] | None:
    """The water-surface elevation measured at the outflow on the bed's datum, or ``None``; never a height above a gauge's own zero."""
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
    """Is this measured level the water over this reach? Over the inflow bed or below the outflow section, yes (the latter refuses by name); in between a horizontal surface would leave the inflow face dry."""
    section = [float(z) for _offset, z in (bed.get("outflow_section") or ())]
    return (level_m > float(bed["bed_top_m"])
            or (bool(section) and level_m <= min(section)))


def _level_opening(bed: Mapping[str, Any], held: tuple[float, str], *,
                   discharge_q: float) -> dict[str, Any]:
    """The opening a reach that does not fall takes: flat at the depth leaving over the outflow section, since a uniform-flow depth needs a fall."""
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


def _refuse_a_dry_inflow_face(roles: Mapping[str, Any], node_bed: Any, level_m: float) -> None:
    """A flat opening the prescribed-flowrate face stands wholly above has no water to impose the inflow on."""
    import numpy as np

    face = np.asarray(node_bed, dtype=float)[[int(n) for n in roles["inflow"]]]
    lowest = float(np.nanmin(face))
    if level_m <= lowest:
        raise TelemacError(
            f"the run opens flat at the derived outflow stage {level_m:.3f} m and the inflow face's lowest node "
            f"stands at {lowest:.3f} m, so the face the discharge is prescribed on opens dry. State a shorter reach "
            "(reach_length_km) or a level that reaches it.", error_code="TELEMAC_INFLOW_FACE_DRY")


def _reported_discharge(carrier: Any) -> tuple[float, str]:
    """The streamflow the carrier observation reports, with what reported it.

    A record that never passed its slot's ingestion is refused; choosing the site is the slot's job.
    """
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
        # A reading with no site and no moment is the stated value; naming a record as its source would name a source nobody read.
        return float(carrier.value), (
            f"the discharge {carrier.value:g} m3/s was stated on the call.")
    return float(carrier.value), (
        f"the discharge {carrier.value:g} m3/s is what "
        f"{where or 'the carrier record'} reported"
        + (f" on {carrier.sampled}" if carrier.sampled else "") + ".")


def _measured_channel(roles: Mapping[str, Any], node_xy: Any,
                      node_bed: Any) -> dict[str, Any]:
    """The two bed medians are over the inflow and outflow runs' nodes; the fall between them over their distance is the friction slope."""
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
    """The level this domain stands at and the depth that leaves.

    ``level`` is a number, an observation, or an addition's measured record. Nothing measured on a
    depth-stated bed is the bed's zero; on a datum bed it is no water, a state and not a refusal.
    """
    import numpy as np

    # A bed stated as a depth is counted from the free surface itself.
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
        # What an open edge holds with no channel to derive a stage: the measured level and its window (a tide moves; one number would hold the sea still).
        measured["outflow_stage_m"] = measured["level_m"]
        measured["outflow_stage_series"] = _window(level)
    measured["opening"] = measured["opening"] or FLAT
    if measured["surface"] is not None:
        surface = float(measured["surface"]["z_in"])
        levels = surface_at(measured["surface"], _points)
    else:
        levels = np.full(bed.shape, surface)
    deepest = surface - floor
    if deepest <= 0.0:
        raise TelemacError(
            f"the free surface opens at {surface:.3f} m and the deepest node of "
            "the accepted mesh sits at or above it, so this domain holds no "
            "water to solve in. State the depth the body holds, or name a bed "
            "measured on the datum the level is read on.",
            error_code="TELEMAC_DOMAIN_HOLDS_NO_WATER")
    wet = levels - bed
    measured["max_depth_m"] = round(float(np.nanmax(wet)), 2)
    if measured["depth_m"] is None:
        measured["depth_m"] = round(float(np.nanmean(wet[wet > 0.0])), 3)
    laid = (f"SLOPED from {measured['surface']['z_in']:.3f} m at the inflow face to "
            f"{measured['surface']['z_out']:.3f} m at the outflow face along the walked line"
            if measured["surface"] is not None else f"FLAT at {measured['level_m']:.3f} m")
    journal_note(
        f"the opening: {laid} over a "
        f"bed the accepted mesh carries between {floor:.3f} m and "
        f"{ceiling:.3f} m, so the deepest column is "
        f"{measured['max_depth_m']:.2f} m and "
        f"{float((wet > 0.0).mean()) * 100.0:.1f}% of the nodes hold water.")
    return measured


async def open_water(
    *,
    mesh: dict[str, Any],
    files: dict[str, Any],
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
    """A body of water opens at a level over its bed: mesh, clock, files, and the opening.

    The depth is the level minus the bed and the edge is wall all round; what a question adds on
    top (an inflow's normal depth, a rating curve) arrives here as the level.
    """
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
    topology = topology_of(files, missing=mesh_missing)
    # A template that states the designating depth has a sea state prescribed across that edge; one that states none may be closed.
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
        # Keywords the settle fills, by the engine's names: the sheet takes each one a module spells that the template does not state.
        "keywords": {"TITLE": f"{slug} DOMAIN", "TIME_STEP": time_step_s,
                     "DURATION": duration_s,
                     "INITIAL_CONDITIONS": opening["opening"],
                     "INITIAL_DEPTH": opening["depth_m"],
                     "INITIAL_ELEVATION": opening["level_m"]},
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
        # The opening as every template reads it; a run with no stated level carries None for all three.
        **opening,
        # Which dataset painted the nodes and where each stopped.
        "bed_source": facts["bed_source"],
        "liquid_boundary_order": list(topology["liquid_boundary_order"]),
        "liquid_boundary_prescribes": list(topology["liquid_boundary_prescribes"]),
        # Where each numbered liquid boundary sits, so a printed flux reads at the one a Point stands on.
        "liquid_boundaries": _liquid_boundaries(topology, node_xy),
        "continue_from": PREVIOUS_DEST if continue_from else None,
        "mesh_inputs": [
            {"gs_uri": geometry_uri(files, missing=mesh_missing),
             "dest": geometry},
            {"gs_uri": boundary_uri(files, missing=mesh_missing),
             "dest": boundary},
            *([{"gs_uri": str(continue_from), "dest": PREVIOUS_DEST}]
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
    files: dict[str, Any],
    friction_law: int,
    friction_coefficient: float,
    carrier: Any = None,
    stage: Any = None,
    domain: Any = None,
) -> Any:
    """A channel opens under a discharge, on top of any body of water.

    A measured level is what the outflow holds: a reach whose inflow derives a higher level opens on the plane between
    them along its walked line, else flat at it; with no measurement the outflow is the normal depth over its section.
    A run with no carrier opens no channel and hands back the level it was given.
    """
    if carrier is None:
        return stage
    node_xy, node_bed = await asyncio.to_thread(mesh_nodes, mesh)
    roles = topology_of(files, missing=mesh_missing)["roles"] or {}
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
    line = (getattr(domain, "companions", None) or {}).get("centerline")
    bed = _measured_channel(roles, node_xy, node_bed)
    surface = None
    if held is not None and line is not None:
        surface = await asyncio.to_thread(
            _sloped, roles, node_xy, node_bed, line, held[0], utm_epsg=mesh_zone(mesh), law=law,
            coefficient=coefficient, discharge_q=inflow_q)
    # A measured level is where the water stands; a uniform-flow depth is a model and does not override it. The derivation serves the reach with no measurement, or one not reaching it.
    if surface is not None:
        normal = _level_opening(bed, held, discharge_q=inflow_q)
        _refuse_a_dry_inflow_face(roles, node_bed, surface["z_in"])
        journal_note(
            f"the open channel: the outflow HOLDS at the measured level {normal['stage_m']:.3f} m, and the run opens "
            f"SLOPED along its walked line from {surface['z_in']:.3f} m at the inflow face - the normal depth "
            f"{inflow_q:g} m3/s is conveyed at over the inflow section at {surface['law']} {coefficient:g} and the "
            f"friction slope {surface['slope']:.6f} (the measured bed falls {surface['drop_m']:.3f} m over "
            f"{surface['length_m']:.1f} m of the walked line) - to the measured level at the outflow face. "
            f"{normal['held']} {discharge_note}")
    elif held is not None and _stands_over_the_reach(bed, held[0]):
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
        _refuse_a_dry_inflow_face(roles, node_bed, normal["stage_m"])
        journal_note(
            f"the open channel: the outflow stage {normal['stage_m']:.3f} m is the "
            f"normal depth {normal['q_m3s']:g} m3/s is conveyed at over the measured "
            f"outflow section at {normal['law']} {normal['coefficient']:g} and the "
            f"friction slope {normal['slope']:.6f}; the run opens FLAT at it and the "
            f"inflow fills the reach to its uniform-flow surface. {discharge_note}")
    return {
        # The level the base opens at: flat at the measured level, or at the derived outflow stage.
        "level_m": round(float(normal["stage_m"]), 3),
        "depth_m": round(float(normal["depth_m"]), 3),
        "opening": FLAT if surface is None else SLOPED,
        "surface": surface,
        "outflow_stage_m": round(float(normal["stage_m"]), 3),
        "inflow_q_m3s": inflow_q,
        # The window each number came out of; the boundary writes the series and the number stands for the rest.
        "inflow_q_series": _window(carrier),
        "outflow_stage_series": _window(stage),
        "friction_law": law,
        "friction_coefficient": coefficient,
        "discharge_note": discharge_note,
        "normal": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in normal.items()},
    }


def _chainage(line: Any, xy: Any) -> Any:
    """How far along ``line`` (N,2) each point of ``xy`` projects, in the line's own metres."""
    import numpy as np

    a, b = line[:-1], line[1:]
    d = b - a
    seg = np.hypot(d[:, 0], d[:, 1])
    start = np.concatenate([[0.0], np.cumsum(seg)[:-1]])
    pts = np.asarray(xy, dtype=float).reshape(-1, 2)
    rel = pts[:, None, :] - a[None, :, :]
    t = np.clip(np.einsum("pkj,kj->pk", rel, d) / np.where(seg > 0.0, seg * seg, 1.0), 0.0, 1.0)
    near = a[None, :, :] + t[:, :, None] * d[None, :, :]
    gap = np.hypot(*(pts[:, None, :] - near).transpose(2, 0, 1))
    k = np.argmin(gap, axis=1)
    return start[k] + t[np.arange(len(pts)), k] * seg[k]


def surface_at(surface: Mapping[str, Any], xy: Any) -> Any:
    """The sloped opening's level at each point: linear in chainage between the two faces, held at each beyond them.
    The engine's routine computes the same from the same line (``surface_routine``)."""
    import numpy as np

    line = np.asarray(surface["line"], dtype=float)
    s = _chainage(line, xy)
    t = np.clip((s - float(surface["s_in"])) / (float(surface["s_out"]) - float(surface["s_in"])), 0.0, 1.0)
    return float(surface["z_in"]) + t * (float(surface["z_out"]) - float(surface["z_in"]))


def _ordered_line(roles: Mapping[str, Any], node_xy: Any, line_source: Any, utm_epsg: int) -> tuple[Any, float, float]:
    """The walked line in the mesh's metres, upstream first, and where the inflow and outflow faces project on it."""
    import numpy as np

    from trid3nt_server.tools.mesh.shared.nodes import read_centerline_utm

    line = np.asarray(read_centerline_utm(line_source, int(utm_epsg)), dtype=float)
    xy = np.asarray(node_xy, dtype=float)
    centres = np.array([xy[[int(n) for n in roles[r]]].mean(axis=0) for r in ("inflow", "outflow")])
    s_in, s_out = _chainage(line, centres)
    if s_out < s_in:
        line = line[::-1]
        s_in, s_out = _chainage(line, centres)
    return line, float(s_in), float(s_out)


def _sloped(roles: Mapping[str, Any], node_xy: Any, node_bed: Any, line_source: Any, held_m: float, *,
            utm_epsg: int, law: int, coefficient: float, discharge_q: float) -> dict[str, Any] | None:
    """The plane a reach held at a measured outflow level opens on, or ``None`` where its inflow derives no higher level.

    The inflow face's level is the normal depth over its own section at the friction slope the measured bed falls
    along the walked line between the two faces."""
    import numpy as np

    from ..helpers.uniform_flow import UniformFlowError

    line, s_in, s_out = _ordered_line(roles, node_xy, line_source, utm_epsg)
    xy, bed = np.asarray(node_xy, dtype=float), np.asarray(node_bed, dtype=float)
    ends = {role: [int(n) for n in roles[role]] for role in ("inflow", "outflow")}
    length = float(s_out - s_in)
    drop = float(np.nanmedian(bed[ends["inflow"]]) - np.nanmedian(bed[ends["outflow"]]))
    section = face_section(ends["inflow"], xy, bed, missing=_reach_section_unmeasured)
    try:
        inflow = normal_depth_stage({"outflow_section": section, "bed_drop_m": drop, "reach_length_m": length},
                                    law=law, coefficient=coefficient, discharge_q=discharge_q)
    except UniformFlowError:
        return None
    if inflow["stage_m"] <= held_m:
        return None
    return {"line": [[round(float(x), 3), round(float(y), 3)] for x, y in line],
            "s_in": round(float(s_in), 3), "s_out": round(float(s_out), 3),
            "z_in": round(float(inflow["stage_m"]), 3), "z_out": round(float(held_m), 3),
            "slope": inflow["slope"], "drop_m": drop, "length_m": length, "law": inflow["law"]}


def surface_routine(surface: Mapping[str, Any]) -> str:
    """The run's ``user_condin_h.f``: the depth under the sloped opening at every node, from the walked line.
    X and Y are the subdomain's own nodes in a partitioned run, so each rank lays its own. Only what it reads is
    use-associated: the module's other names (T, S) would shadow its locals."""
    vertices = "\n".join(f"      XL({k})={x:.3f}D0\n      YL({k})={y:.3f}D0"
                          for k, (x, y) in enumerate(surface["line"], start=1))
    return f"""                    SUBROUTINE USER_CONDIN_H
      USE BIEF
      USE DECLARATIONS_TELEMAC2D, ONLY: H, ZF, X, Y, NPOIN
      IMPLICIT NONE
      INTEGER, PARAMETER :: NL = {len(surface["line"])}
      DOUBLE PRECISION XL(NL), YL(NL), SL(NL)
      DOUBLE PRECISION, PARAMETER :: S_IN = {float(surface["s_in"]):.3f}D0
      DOUBLE PRECISION, PARAMETER :: S_OUT = {float(surface["s_out"]):.3f}D0
      DOUBLE PRECISION, PARAMETER :: Z_IN = {float(surface["z_in"]):.3f}D0
      DOUBLE PRECISION, PARAMETER :: Z_OUT = {float(surface["z_out"]):.3f}D0
      DOUBLE PRECISION BEST, D2, T, DX, DY, L2, S, ZS, PX, PY
      INTEGER I, K
{vertices}
      SL(1) = 0.D0
      DO K = 2, NL
        DX = XL(K) - XL(K-1)
        DY = YL(K) - YL(K-1)
        SL(K) = SL(K-1) + SQRT(DX*DX + DY*DY)
      ENDDO
      DO I = 1, NPOIN
        BEST = HUGE(1.D0)
        S = 0.D0
        DO K = 1, NL - 1
          DX = XL(K+1) - XL(K)
          DY = YL(K+1) - YL(K)
          L2 = DX*DX + DY*DY
          T = 0.D0
          IF (L2.GT.0.D0) T = ((X(I)-XL(K))*DX + (Y(I)-YL(K))*DY) / L2
          T = MAX(0.D0, MIN(1.D0, T))
          PX = X(I) - XL(K) - T*DX
          PY = Y(I) - YL(K) - T*DY
          D2 = PX*PX + PY*PY
          IF (D2.LT.BEST) THEN
            BEST = D2
            S = SL(K) + T*SQRT(L2)
          ENDIF
        ENDDO
        T = MAX(0.D0, MIN(1.D0, (S-S_IN) / (S_OUT-S_IN)))
        ZS = Z_IN + T*(Z_OUT-Z_IN)
        H%R(I) = MAX(0.D0, ZS - ZF%R(I))
      ENDDO
      RETURN
      END
"""
