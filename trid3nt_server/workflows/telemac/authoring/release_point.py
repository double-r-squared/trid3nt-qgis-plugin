"""THE SETTLED RELEASE POINT: where on the accepted mesh a source enters water.

An unplaced point sits its fraction along the domain's own centerline and walks
downstream to the first station the triangulation holds; a supplied one is held
inside the domain and moved onto the flowline. Either way it lands on a node the
run's own initial state has water at, off the rim, because the solver reads a
source on the rim as outside the domain."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from trid3nt_server.workflows.runtime import journal_note
from trid3nt_server.workflows.mesh.shared.nodes import (
    accepted_mesh_nodes,
    read_centerline_utm,
)
from trid3nt_server.inputs.point import Point, as_utm, contain, publish_point, snap_to_wet
from ..errors import TelemacError
from .accepted_mesh import slug_of
from .opening import initial_state_of

__all__ = ["settle_release"]

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.release_point")


def _to_lonlat_point(xy: tuple[float, float],
                     utm_epsg: int) -> tuple[float, float]:
    """One point in the mesh's own metres back in lon/lat."""
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform(
        float(xy[0]), float(xy[1]))
    return (float(lon), float(lat))


def _domain_polygon(artifact: Any) -> Any:
    """The polygon the accepted mesh was cut from, or a typed refusal.

    A mesh cut from a bbox carries no polygon and is refused, never approximated."""
    # The mesh records the RECIPE it was built from, so the domain a containment
    # test runs against is the mesh's own statement of it rather than a second
    # resolution of the same question. A point tested against four numbers is a
    # point nobody tested.
    recipe = ((getattr(artifact, "provenance", None) or {}).get("recipe") or {})
    extent = recipe.get("extent")
    if isinstance(extent, (tuple, list)) or extent is None:
        raise TelemacError(
            f"the accepted mesh for this run was cut from {extent!r} rather than "
            "from a domain polygon, so there is no mapped shape a source point "
            "could be inside of. A reach is meshed from the sectioned water "
            "polygon; solve on a mesh built that way.",
            error_code="TELEMAC_DOMAIN_NOT_A_POLYGON")
    return extent


def _station_on_mesh(*, centerline_utm: Any, mesh: Any,
                     fraction: float) -> tuple[tuple[float, float], str | None]:
    """A DERIVED source: ``fraction`` along the centerline, inside the mesh.

    Returns ``((lon, lat), note)`` in EPSG:4326, the note ``None`` if unmoved."""
    # The centerline is the whole navigated stretch; the accepted mesh is only the
    # part of it the mapped banks and the cleanup left, so a station on the line is
    # not a station in the domain. A source the solver cannot find an element for
    # stops the run at startup with nothing but "SOURCE POINT OUTSIDE DOMAIN", so
    # the station is walked DOWNSTREAM to the first one the triangulation holds and
    # the distance it travelled is said out loud.
    import numpy as np
    import shapely
    from pyproj import Transformer
    from shapely.geometry import LineString

    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    display_uri = str(mesh.get("display_uri") or "")
    line = LineString(centerline_utm)
    frac = min(max(float(fraction), 0.0), 1.0)
    start = frac * line.length
    back = Transformer.from_crs(int(utm_epsg or 4326), 4326, always_xy=True)

    points_utm, cells, _bed, _lonlat = read_accepted_mesh_nodes(
        display_uri, utm_epsg=utm_epsg)
    rings = np.asarray(points_utm, dtype=float)[np.asarray(cells, dtype=np.int64)]
    tree = shapely.STRtree(
        shapely.polygons(np.concatenate([rings, rings[:, :1]], axis=1)))
    # A cell-length stride: finer than that resolves nothing the mesh can hold,
    # coarser than that could step over a short meshed stretch entirely.
    step = max(float(line.length) / 2000.0, 1.0)
    walked = 0.0
    while start + walked <= line.length:
        here = line.interpolate(start + walked)
        if tree.query(here, predicate="intersects").size:
            lon, lat = back.transform(here.x, here.y)
            note = (None if walked <= 0.0 else
                    f"derived station moved {walked:.0f} m downstream to the "
                    "first point the accepted mesh holds")
            if note:
                logger.info("derived source walked %.0f m downstream into the "
                            "meshed reach", walked)
            return (float(lon), float(lat)), note
        walked += step
    raise TelemacError(
        f"no point on the centerline at or below {frac:.0%} of its length lies "
        "inside the accepted mesh, so there is nowhere in the solved domain to "
        "put the source. Mesh more of the reach (a finer mesh_resolution_m or a "
        "supplied mesh) or place the point explicitly.",
        error_code="TELEMAC_SOURCE_OFF_MESH")


def _inside_domain(point: Point, polygon: Any, label: str) -> Point:
    """Refuse a point the meshed domain does not hold; hold one it does.

    What a domain with no channel through it can say about a placed point: it is
    inside or it is not, and moving it would be this step choosing the place."""
    from shapely.geometry import Point as _P, shape as _shape
    from shapely.ops import transform as _transform
    from pyproj import Transformer

    from trid3nt_server.inputs.geometry import (
        flatten_geometries, read_geometry_doc, utm_epsg_for,
    )
    from trid3nt_server.inputs.point import PointOutsideDomainError

    shapes = [_shape(g) for g in flatten_geometries(read_geometry_doc(polygon))
              if str(g.get("type")) in ("Polygon", "MultiPolygon")]
    if not shapes:
        raise TelemacError(
            f"the domain carries no polygon, so there is no shape the {label} "
            "could be inside of.", error_code="TELEMAC_DOMAIN_NOT_A_POLYGON")
    from shapely.ops import unary_union

    domain_ll = unary_union(shapes).buffer(0)
    epsg = utm_epsg_for(float(domain_ll.centroid.x), float(domain_ll.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    domain_m = _transform(forward.transform, domain_ll)
    here = _P(*forward.transform(point.lon, point.lat))
    if not domain_m.covers(here):
        raise PointOutsideDomainError(point, label=label,
                                      distance_m=float(here.distance(domain_m)))
    return point


def _inside_water(cells: Any, wet: Any) -> Any:
    """The nodes a SOURCE may enter the water at: wet at t0, and not on the edge."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.nodes import interior_nodes

    held = np.asarray(wet, dtype=bool)
    return held & interior_nodes(cells, held.shape[0])


async def _settle_release(
    point: Point | None, *, mesh: dict[str, Any],
    centerline: Any, centerline_utm: Any, utm_epsg: int, fraction: float,
    node_xy: Any, initial_state: Mapping[str, Any], label: str,
) -> tuple[Point, str]:
    """WHERE the source enters the water -> the settled Point and how it was decided.

    A supplied point the domain polygon does not hold raises rather than moves."""
    # With no point placed the source sits at ``fraction`` along the domain's own
    # CENTERLINE companion, walked downstream to the first station the ACCEPTED
    # MESH holds: the centerline is the whole navigated stretch and the mesh is
    # only the part of it the mapped banks left, so "on the line" and "in the
    # domain" are two different claims and only the second one solves.
    if point is None:
        if centerline_utm is None:
            raise TelemacError(
                f"this domain offers no centerline for the {label} to be placed "
                "along, so where the substance enters the water is not something "
                "the run can derive. Place the point.",
                error_code="TELEMAC_RELEASE_UNPLACED")
        (lon, lat), note = await asyncio.to_thread(
            _station_on_mesh, centerline_utm=centerline_utm, mesh=mesh,
            fraction=fraction)
        placed = Point(lon, lat)
    elif centerline is None:
        placed = await asyncio.to_thread(
            _inside_domain, point, _domain_polygon(mesh.get("artifact")), label)
        note = "supplied point, inside the modeled domain"
    else:
        placed, moved_m = await asyncio.to_thread(
            contain, point, domain=_domain_polygon(mesh.get("artifact")),
            flowline=centerline, label=label)
        note = ("supplied point, inside the modeled domain and on the flowline"
                if moved_m <= 0.0 else
                f"supplied point, inside the modeled domain; moved {moved_m:.0f} m "
                "onto the flowline")

    # The settled point lands LAST on the nearest node a source may enter the
    # water at. Both steps above ask about geometry only; a bankfull domain at
    # low flow has mapped river that is dry ground when the run opens, and a
    # source released onto it discharges into the bed.
    wet_utm, moved_m, node = await asyncio.to_thread(
        snap_to_wet, as_utm(placed, utm_epsg),
        node_xy=node_xy, wet=initial_state["wet"], state=initial_state["note"],
        label=label)
    settled = (f"solved at mesh node {node}, {moved_m:.1f} m from where it was "
               f"placed - the nearest node, which holds water at t0 "
               f"({initial_state['note']})")
    logger.info("%s settled: %s", label, settled)
    journal_note(f"{label}: {settled}."
                 + (f" Before that: {note}." if note else ""))
    lon, lat = _to_lonlat_point(wet_utm, utm_epsg)
    return Point(lon, lat, placed.name), "; ".join(
        part for part in (note, settled) if part)


async def settle_release(
    *,
    point: Point | None,
    mesh: dict[str, Any],
    label: str,
    domain: Any = None,
    fraction: float = 0.5,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Where a source enters the water, settled against the ACCEPTED mesh.

    A supplied point is held inside the domain - and onto the channel where the
    domain has a CENTERLINE companion; an unplaced one sits ``fraction`` along
    that centerline. Both land on the nearest node holding water at t0, and a
    domain with no centerline and no placed point refuses rather than guessing."""
    from trid3nt_server.render.pipeline_emitter import current_emitter

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    # The producer's own centerline runs head-to-tail the way the water does, so
    # ``fraction`` counts from upstream without a seed to orient it.
    centerline = getattr(domain, "companions", {}).get("centerline")
    centerline_utm = None if centerline is None else await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg)
    node_xy, cells, _bed, _lonlat = await asyncio.to_thread(
        accepted_mesh_nodes, mesh)
    initial_state = await asyncio.to_thread(initial_state_of, continue_from, len(node_xy))
    # WHERE A SOURCE MAY ENTER: wet at t0, and off the edge - the mesh's own rim
    # is where boundary conditions are imposed, and a source placed on it is one
    # the solver reads as outside the domain it solves.
    initial_state = {**initial_state,
                     "wet": _inside_water(cells, initial_state["wet"])}
    placed, note = await _settle_release(
        point, mesh=mesh, centerline=centerline, centerline_utm=centerline_utm,
        utm_epsg=utm_epsg, fraction=fraction, node_xy=node_xy,
        initial_state=initial_state, label=label)
    # The marker rides BEFORE the solve, so the user sees the input against the
    # mesh rather than only in the results, and it carries the SETTLED point.
    await publish_point(current_emitter(), placed, label=label,
                        basis="user" if point is not None else "derived",
                        context=slug_of(getattr(domain, "name", None)
                                      or str(mesh.get("mesh_id") or "domain")))
    at = as_utm(placed, utm_epsg)
    return {"at": [round(at[0], 3), round(at[1], 3)],
            "lon": round(placed.lon, 6), "lat": round(placed.lat, 6),
            "name": placed.name, "user_supplied": point is not None,
            "note": note, "fraction": float(min(max(fraction, 0.0), 1.0))}
