"""THE RATING CURVE the outlet holds: a stage for every flow the storm can send.

The normal depth over the section the outlet's own face cuts, swept over that
flow range at the roughness the deck writes at those same nodes - a level read
off another law is a level the run never sits at."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from trid3nt_server.workflows.runtime import journal_note
from trid3nt_server.mesh.shared.nodes import (
    accepted_mesh_nodes,
    sample_layer_at_nodes,
)
from .topology import RATING_CURVE_ROLE
from ..errors import TelemacError
from .accepted_mesh import face_section, mesh_missing, topology_of

__all__ = ["settle_outlet_rating"]

#: The mesh boundary ROLE a catchment's outlet carries. Its quad prescribes a
#: LEVEL and the level comes from the run's own derived stage-discharge curve, so
#: the outlet stands where the flow leaving it says it stands rather than at a
#: constant nobody measured. The hydrograph is the flux across the nodes that
#: took the role.
_OUTLET_ROLE = RATING_CURVE_ROLE


def _outlet_section_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_OUTLET_SECTION_UNMEASURED")


def _outlet_boundary(files: Mapping[str, Any]
                     ) -> tuple[dict[str, Any], int, str, int]:
    """The declared OUTLET: ``(topology, number, what its quad prescribes, count)``.

    The number is the solver's own liquid-boundary walk order, 1-based."""
    topology = topology_of(files, missing=mesh_missing)
    order = list(topology["liquid_boundary_order"])
    if _OUTLET_ROLE not in topology["roles"] or _OUTLET_ROLE not in order:
        raise TelemacError(
            f"no boundary node of the catchment mesh took the {_OUTLET_ROLE!r} "
            "role, so the basin has no outlet to drain through and no hydrograph "
            "to measure. Move the pour point onto the basin's own outlet, or mesh "
            "it finer so a boundary node reaches it.",
            error_code="TELEMAC_OUTLET_UNSET")
    # The solver numbers its liquid boundaries by walking the geometry and prints
    # one flux per number in its own volume balance; the accepted topology recorded
    # that numbering when the ``.cli`` was written. So the number is what turns
    # "the outlet" into the series the hydrograph reads, and the count is what lets
    # the deck state one stage-discharge entry per boundary in the same numbering.
    number = order.index(_OUTLET_ROLE) + 1
    return (topology, number,
            str(topology["liquid_boundary_prescribes"][number - 1]), len(order))


def _bed_slope(nodes: Sequence[int], node_xy: Any, node_bed: Any,
               cells: Any) -> float:
    """The bed gradient at a face, over the ELEMENTS that face's nodes belong to.

    The plane is fitted through the painted nodes of the touching elements."""
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    patch = np.unique(tri[np.isin(tri, np.asarray(list(nodes),
                                                  dtype=np.int64)).any(axis=1)])
    patch = patch[np.isfinite(bed[patch])]
    if patch.size < 3:
        raise TelemacError(
            f"the outlet face touches {patch.size} painted mesh node(s), which is "
            "no ground to measure a bed slope over; a catchment mesh paints its "
            "bed with set_bed.",
            error_code="TELEMAC_OUTLET_SLOPE_UNMEASURED")
    plane = np.linalg.lstsq(
        np.column_stack([xy[patch, 0], xy[patch, 1], np.ones(patch.size)]),
        bed[patch], rcond=None)[0]
    slope = float(np.hypot(plane[0], plane[1]))
    if not (slope > 0.0):
        raise TelemacError(
            f"the bed over the {patch.size} nodes around the outlet is flat "
            f"(gradient {slope:.6g}), so there is no uniform-flow depth for the "
            "outlet to hold and its level would have to come from a gauge.",
            error_code="TELEMAC_OUTLET_SLOPE_UNMEASURED")
    return slope


def _outlet_nodes(topology: Mapping[str, Any]) -> list[int]:
    return [int(n) for n in (topology["roles"].get(_OUTLET_ROLE) or ())]


def _outlet_manning(landcover: Any, lonlat: Any, roughness: Mapping[Any, Any],
                    unmapped: Any) -> list[float]:
    """The Manning n at the outlet nodes, read off the land cover the way the
    infiltration surface reads it, so the curve is derived under the roughness
    the deck writes at those nodes."""
    table = {int(code): tuple(row) for code, row in dict(roughness).items()}
    return [float(table.get(int(round(float(code))), tuple(unmapped))[1])
            for code in sample_layer_at_nodes(landcover, lonlat)]


def _measured_outlet(topology: Mapping[str, Any], node_xy: Any, node_bed: Any,
                     cells: Any, manning: Any, *, law: int,
                     q_ceiling_m3s: float, q_ceiling_basis: str) -> dict[str, Any]:
    """What the accepted mesh says about the face the basin drains through.

    Section, slope and roughness, every one measured off the artifact itself;
    ``manning`` is the roughness at the outlet's own nodes."""
    import numpy as np

    nodes = _outlet_nodes(topology)
    coefficient = float(np.nanmedian(np.asarray(manning, dtype=float)))
    if not np.isfinite(coefficient) or coefficient <= 0.0:
        raise TelemacError(
            f"the friction field carries {coefficient!r} at the outlet nodes, so "
            "the outlet's rating curve has no roughness to be derived under.",
            error_code="TELEMAC_OUTLET_FRICTION_UNMEASURED")
    return {
        "section": face_section(nodes, node_xy, node_bed,
                                 missing=_outlet_section_unmeasured),
        "slope": _bed_slope(nodes, node_xy, node_bed, cells),
        "law": law, "coefficient": coefficient,
        "q_ceiling_m3s": float(q_ceiling_m3s),
        "q_ceiling_basis": q_ceiling_basis,
    }


def _rain_ceiling(rain: Mapping[str, Any], cells: Any,
                  node_xy: Any) -> tuple[float, str]:
    """The most the outlet can ever discharge, and the basis of that number.

    Gross rain on the meshed area caps it: infiltration only removes water."""
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    a, b, c = xy[tri[:, 0]], xy[tri[:, 1]], xy[tri[:, 2]]
    area_m2 = float(0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                                 - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])).sum())
    # A measured record is gross millimetres over each of its own hourly blocks;
    # a design storm is the engine's own keyword, in millimetres per day. Both
    # reach the ceiling as a depth per second over the meshed area.
    peak_m_per_s = (max(float(v) for v in rain["series"]) / 1000.0 / 3600.0
                    if rain.get("kind") == "hyetograph"
                    else float(rain["mm_per_day"] or 0.0) / 1000.0 / 86400.0)
    peak_mm_hr = peak_m_per_s * 1000.0 * 3600.0
    if not (peak_m_per_s > 0.0 and area_m2 > 0.0):
        raise TelemacError(
            f"a storm of {peak_mm_hr:g} mm/h over {area_m2:g} m2 puts no water on "
            "the catchment, so there is no flow range for the outlet's rating "
            "curve to span.",
            error_code="TELEMAC_STORM_EMPTY")
    ceiling = peak_m_per_s * area_m2
    return ceiling, (
        f"the gross rain rate on the meshed catchment - peak {peak_mm_hr:g} mm/h "
        f"over {area_m2 / 1.0e6:.3f} km2 is {ceiling:.3f} m3/s, which no outlet "
        "flux can exceed because infiltration only removes water and storage "
        "only delays it")


async def settle_outlet_rating(
    *,
    mesh: dict[str, Any],
    files: dict[str, Any],
    landcover: Any,
    roughness: Mapping[Any, Any],
    unmapped: Any,
    friction_law: int,
    mm_per_day: float | None = None,
    series: Any = None,
    record: Any = None,
) -> dict[str, Any]:
    """The stage-discharge curve the catchment's outlet holds, and which boundary
    reads it.

    The section, the bed slope and the roughness are measured off the accepted
    mesh itself; the flow range is the gross rain on the meshed area, which
    nothing leaving it can exceed, because infiltration only removes water and
    storage only delays it. ``friction_law`` is the deck's own LAW OF BOTTOM
    FRICTION: a curve derived under a roughness the run is not solved at is a
    level the run never sits at."""
    topology, outlet_boundary, outlet_prescribes, n_liquid = _outlet_boundary(files)
    if outlet_prescribes != "elevation":
        raise TelemacError(
            f"liquid boundary {outlet_boundary} carries a .cli code quad that "
            f"prescribes {outlet_prescribes!r}, and a stage-discharge curve is "
            "read only where the depth is prescribed; the boundary file and the "
            "steering file would describe different outlets.",
            error_code="TELEMAC_BOUNDARY_PRESCRIBES_NOTHING")
    points_utm, cells, node_bed, lonlat = await asyncio.to_thread(
        accepted_mesh_nodes, mesh)
    measured = series if series is not None else record
    storm = ({"kind": "hyetograph", "series": list(measured)} if measured
             else {"kind": "design_storm", "mm_per_day": mm_per_day})
    q_ceiling, q_ceiling_basis = _rain_ceiling(storm, cells, points_utm)
    manning = await asyncio.to_thread(
        _outlet_manning, landcover, lonlat[_outlet_nodes(topology)], roughness,
        unmapped)
    outlet = _measured_outlet(
        topology, points_utm, node_bed, cells, manning, law=int(friction_law),
        q_ceiling_m3s=q_ceiling, q_ceiling_basis=q_ceiling_basis)
    from ..helpers.uniform_flow import derive_rating_curve

    rating = derive_rating_curve(
        outlet["section"], law=int(outlet["law"]),
        coefficient=float(outlet["coefficient"]), slope=float(outlet["slope"]),
        q_ceiling_m3s=float(outlet["q_ceiling_m3s"]))
    note = (f"derived Z(Q) at liquid boundary {outlet_boundary}: normal depth "
            f"over the measured outlet section at {rating['law']} "
            f"{rating['coefficient']:g}, bed slope {rating['slope']:.6f}, "
            f"{outlet['q_ceiling_basis']}")
    journal_note(
        f"catchment outlet: liquid boundary {outlet_boundary} holds a DERIVED "
        f"stage-discharge curve - {len(rating['rows'])} points from the dry "
        f"section at {rating['thalweg_m']:.3f} m to {rating['stage_max_m']:.3f} m "
        f"at {rating['q_ceiling_m3s']:.3f} m3/s, each a normal depth over the "
        f"measured outlet section at {rating['law']} {rating['coefficient']:g} on "
        f"the measured bed slope {rating['slope']:.6f}. The range is "
        f"{outlet['q_ceiling_basis']}.")
    return {"at_boundary": outlet_boundary, "of_boundaries": n_liquid,
            "rows": [[q, z] for q, z in rating["rows"]], "note": note}
