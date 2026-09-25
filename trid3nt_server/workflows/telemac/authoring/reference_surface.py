"""THE REFERENCE SURFACE a dredger cuts to: the grade, as NESTOR reads levels.

The design grade laid out as cross-sections along the domain's own centerline,
stationed downstream from the end the inflow names, and the stock under it
measured against the bed before the run dispatches - the engine only refuses a
dredger with nothing left to cut part-way through its first pass."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from trid3nt_server.workflows.runtime import journal_note
from trid3nt_server.mesh.shared.nodes import read_centerline_utm
from ..errors import TelemacError
from .accepted_mesh import mesh_nodes, to_utm
from .opening import FLAT

__all__ = ["settle_dredge"]

#: How far past the outermost node a reference profile reaches, as a fraction of
#: what it spans. The profiles are a SURFACE the engine interpolates every node
#: of a field onto, and a node outside the quadrangle two neighbouring profiles
#: bound reads no level at all - so the band overhangs the mesh on all four
#: sides rather than ending on it.
_PROFILE_MARGIN = 1.25


#: How close two profiles may stand, as a multiple of their own half-width. Two
#: perpendiculars to a bending line converge on the inside of the bend, and a
#: pair that crosses inside the band bounds a quadrangle turned inside out; at
#: this spacing they can only cross where the reach turns more than fifty
#: degrees between them.
_PROFILE_SPACING = 2.0


#: The fewest profiles the reader takes, and the most a reach is described with:
#: the surface between them is linear, so past a handful the extra lines state
#: nothing the interpolation does not already carry.
_PROFILE_MIN, _PROFILE_MAX = 2, 12


#: The fewest profiles the reader takes, and the most a reach is described with:
#: the surface between them is linear, so past a handful the extra lines state
#: nothing the interpolation does not already carry.
_PROFILE_MIN, _PROFILE_MAX = 2, 12


def _dredge_head(domain: Any) -> tuple[float, float] | None:
    """Which end of the centerline is upstream, off the domain's own inflow run.

    Without one the merged direction stands, and the stationing is whichever way
    the line was drawn."""
    for run in getattr(domain, "runs", ()) or ():
        if getattr(run, "type", "") == "inflow":
            return (float(run.start.lon), float(run.start.lat))
    return None


def _dredge_field(source: Any, name: str, *, utm_epsg: int,
                  node_xy: Any) -> dict[str, Any]:
    """One drawn area -> the polygon the engine works in, in the mesh's metres.

    An area holding no mesh node is refused: nothing in it could be moved."""
    import numpy as np
    from shapely import contains_xy
    from shapely.geometry import Polygon

    geometry = to_utm(source, utm_epsg)
    parts = sorted(getattr(geometry, "geoms", [geometry]),
                   key=lambda g: g.area, reverse=True)
    if not parts or parts[0].area <= 0.0:
        raise TelemacError(
            f"the {name} area carries no polygon, so it bounds nothing to work "
            "on. Draw the area on the canvas or supply a polygon layer.",
            error_code="TELEMAC_DREDGE_AREA_EMPTY")
    # The engine reads a field as ONE closed ring, so both the file and the guard
    # take that ring: a bent or rotated area whose bounding box holds nodes its
    # interior does not would otherwise pass here and find nothing at the solve.
    worked = Polygon(parts[0].exterior)
    ring = [[round(float(x), 3), round(float(y), 3)]
            for x, y in worked.exterior.coords]
    xy = np.asarray(node_xy, dtype=float)
    inside = np.asarray(contains_xy(worked, xy[:, 0], xy[:, 1]))
    if not bool(inside.any()):
        raise TelemacError(
            f"the {name} area holds no node of the mesh this run solves on, so "
            "the engine would find nothing in it to move. Draw it over the "
            "modelled reach.", error_code="TELEMAC_DREDGE_AREA_OFF_MESH")
    return {"label": name, "vertices": ring}


def _refuse_a_cut_past_the_stock(ring: Sequence[Sequence[float]], *,
                                 node_xy: Any, node_bed: Any, name: str,
                                 level_m: float, depth_m: float | None,
                                 grade_depth_m: float, stock_m: float) -> None:
    """The deepest cut the stated grade asks for, against the erodible stock.

    A dredger cannot cut below the layer the deck gives it, and the engine says
    so only once it has solved far enough to try, so the arithmetic is done here
    with the node, its bed, the grade and the stock all named."""
    import numpy as np
    from shapely import contains_xy
    from shapely.geometry import Polygon

    xy = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    inside = np.flatnonzero(
        np.asarray(contains_xy(Polygon(ring), xy[:, 0], xy[:, 1])))
    # The reference is the surface the run opens on, read the same way the
    # profiles below read it: the stated level where it is flat, the local bed
    # plus the normal depth where it follows the bed.
    reference = (np.full(inside.size, level_m) if depth_m is None
                 else bed[inside] + depth_m)
    cut = bed[inside] - (reference - grade_depth_m)
    at = int(np.argmax(cut))
    if float(cut[at]) <= stock_m:
        return
    node = int(inside[at])
    raise TelemacError(
        f"the dredge holds the {name} at {float(reference[at]) - grade_depth_m:.3f} "
        f"m - {grade_depth_m:.3f} m under the surface the run opens on - and node "
        f"{node + 1} of the mesh sits at {float(bed[node]):.3f} m, so the pass "
        f"asks a cut of {float(cut[at]):.3f} m out of the {stock_m:.3f} m of "
        "erodible bed the deck states. Raise the grade to one this channel has "
        "the material to reach, or state a stock the cut fits inside.",
        error_code="TELEMAC_DREDGE_CUT_PAST_STOCK")


def _chainage(xy: Any, line: Any, segment: Any, length: Any,
              cumulative: Any) -> tuple[Any, Any]:
    """Each point's arc length ALONG the line and its distance OFF it."""
    import numpy as np

    t = np.clip(((xy[:, None, 0] - line[None, :-1, 0]) * segment[None, :, 0]
                 + (xy[:, None, 1] - line[None, :-1, 1]) * segment[None, :, 1])
                / (length * length)[None, :], 0.0, 1.0)
    foot_x = line[None, :-1, 0] + t * segment[None, :, 0]
    foot_y = line[None, :-1, 1] + t * segment[None, :, 1]
    distance = np.hypot(foot_x - xy[:, None, 0], foot_y - xy[:, None, 1])
    nearest = np.argmin(distance, axis=1)
    rows = np.arange(xy.shape[0])
    return (cumulative[nearest] + t[rows, nearest] * length[nearest],
            distance[rows, nearest])


def _on_line(line: Any, segment: Any, length: Any, cumulative: Any,
             where: float) -> tuple[Any, Any]:
    """The point at arc length ``where`` and the unit tangent there.

    Past either end the end segment is extended, which is how a profile comes to
    stand outside the mesh it has to overhang."""
    import numpy as np

    index = int(np.clip(np.searchsorted(cumulative, where) - 1, 0,
                        len(length) - 1))
    t = (where - cumulative[index]) / length[index]
    return line[index] + t * segment[index], segment[index] / length[index]


def _reference_profiles(centerline_utm: Any, *, node_xy: Any, node_bed: Any,
                        depth_m: float | None, level_m: float | None
                        ) -> list[list[float]]:
    """The reference surface as the engine reads it: seven reals per profile.

    A band of cross-sections down the reach at the water surface the run opens
    at - the stated level where that surface is flat, the local bed plus the
    depth where it follows the bed - overhanging the mesh at both ends and both
    sides so every node of a field lies inside one pair."""
    import numpy as np

    line = np.asarray(centerline_utm, dtype=float)
    xy = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    segment = line[1:] - line[:-1]
    length = np.maximum(np.hypot(segment[:, 0], segment[:, 1]), 1e-9)
    cumulative = np.concatenate([[0.0], np.cumsum(length)])
    station, offset = _chainage(xy, line, segment, length, cumulative)
    half = float(np.abs(offset).max()) * _PROFILE_MARGIN
    reach = float(station.max() - station.min()) * _PROFILE_MARGIN
    count = int(np.clip(round(reach / max(half * _PROFILE_SPACING, 1e-9)),
                        _PROFILE_MIN, _PROFILE_MAX))
    margin = (reach - (station.max() - station.min())) / 2.0
    rows: list[list[float]] = []
    for where in np.linspace(float(station.min()) - margin,
                             float(station.max()) + margin, count):
        point, axis = _on_line(line, segment, length, cumulative, float(where))
        normal = np.array([-axis[1], axis[0]], dtype=float)
        node = int(np.argmin(np.hypot(xy[:, 0] - point[0], xy[:, 1] - point[1])))
        level = (float(level_m) if depth_m is None
                 else float(bed[node]) + float(depth_m))
        left, right = point + normal * half, point - normal * half
        rows.append([round(float(left[0]), 3), round(float(left[1]), 3),
                     round(level, 4),
                     round(float(right[0]), 3), round(float(right[1]), 3),
                     round(level, 4),
                     round(float(where - station.min()) / 1000.0, 4)])
    return rows


async def settle_dredge(*, mesh: dict[str, Any], line: Any, domain: Any,
                        settled: Mapping[str, Any], areas: Mapping[str, Any],
                        dug_area: str, grade_depth_m: float,
                        stock_m: float) -> dict[str, Any]:
    """The areas a dredge works on and the surface its levels are read from,
    both measured against the ACCEPTED mesh.

    ``areas`` is ``{name: geometry source}``; each comes back as the polygon in
    the mesh's own metres; ``line`` is the LINE the profiles are stationed along,
    and the reference is the run's OWN opening water surface. ``dug_area`` names
    the one the dig works in, and the cut it asks for is measured here against
    the stock the deck states."""
    utm_epsg = int(settled["utm_epsg"])
    node_xy, node_bed = await asyncio.to_thread(mesh_nodes, mesh)
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, line, utm_epsg, start_lonlat=_dredge_head(domain))
    fields = {name: await asyncio.to_thread(
                  _dredge_field, source, name, utm_epsg=utm_epsg, node_xy=node_xy)
              for name, source in areas.items() if source is not None}
    flat = str(settled.get("opening") or "") == FLAT
    await asyncio.to_thread(
        _refuse_a_cut_past_the_stock, fields[dug_area]["vertices"],
        node_xy=node_xy, node_bed=node_bed, name=dug_area,
        level_m=float(settled["level_m"]),
        depth_m=None if flat else float(settled["depth_m"]),
        grade_depth_m=float(grade_depth_m), stock_m=float(stock_m))
    profiles = await asyncio.to_thread(
        _reference_profiles, centerline_utm, node_xy=node_xy, node_bed=node_bed,
        depth_m=None if flat else float(settled["depth_m"]),
        level_m=float(settled["level_m"]))
    journal_note(
        f"dredge reference surface: {len(profiles)} profiles across the reach at "
        + (f"the flat {float(settled['level_m']):.3f} m the water stands at"
           if flat else
           f"the bed the mesh carries plus the {float(settled['depth_m']):.3f} m "
           "normal depth the run opens at")
        + ", which is the water surface the run opens on; every level a dredging "
        "action states is read from it.")
    return {**fields, "profiles": profiles}
