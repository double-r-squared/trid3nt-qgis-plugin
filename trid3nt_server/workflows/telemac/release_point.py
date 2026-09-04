"""Where a release is allowed to be: inside the domain, on the river, in water.

A release point is a claim about where the substance enters the water, and three
things make it a solvable claim - it has to lie inside the DOMAIN the run is
solved over, it has to sit ON the river rather than beside it, and it has to land
where the run actually holds water at t0. All three are answered here, on the
server, before anything is staged: the domain polygon and the flowline are real
fetched geometry the run already holds and the initial state is the one the deck
declares, so a point that cannot be honored is refused while the user can still
move it, and a point that can is moved by the shortest step there is.

Nothing here invents a tolerance. A band around the flowline would be a second,
softer domain standing in for the one the run actually has, and a point it
admitted would be a release inside a shape nobody mapped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trid3nt_server.workflows.telemac.helpers.errors import (
    TelemacDyeScenarioError,
    TelemacReleaseOutsideDomainError,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.release_point")

__all__ = ["ContainedRelease", "contain_release_point", "derive_release_on_mesh",
           "domain_polygon_of", "snap_release_to_wetted"]


@dataclass(frozen=True)
class ContainedRelease:
    """A release point the domain accepts, moved onto the flowline.

    ``snap_distance_m`` is how far the supplied point travelled to reach the
    river; 0 when it was already on it. The note is what the map layer and the
    provenance row say out loud, so a moved point never reads as a placed one.
    """

    lon: float
    lat: float
    snap_distance_m: float

    @property
    def note(self) -> str:
        if self.snap_distance_m <= 0.0:
            return "supplied point, inside the modeled domain and on the flowline"
        return (f"supplied point, inside the modeled domain; moved "
                f"{self.snap_distance_m:.0f} m onto the flowline")


def domain_polygon_of(artifact: Any) -> Any:
    """The polygon the accepted mesh was cut from, or a typed refusal.

    The mesh records the RECIPE it was built from, so the domain a containment
    test has to be against is the mesh's own statement of it rather than a second
    resolution of the same question.

    There is ONE path. A mesh cut from a bbox is four numbers and no polygon, and
    a release point tested against nothing is a release point nobody tested: the
    reach templates declare a polygon domain, so a mesh without one is a mesh this
    run was never meant to solve on and says so here.
    """
    recipe = ((getattr(artifact, "provenance", None) or {}).get("recipe") or {})
    extent = recipe.get("extent")
    if isinstance(extent, (tuple, list)) or extent is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the accepted mesh for this run was cut from {extent!r} rather than "
            "from a domain polygon, so there is no mapped shape a release point "
            "could be inside of. A reach is meshed from the sectioned water "
            "polygon; solve on a mesh built that way.")
    return extent


def derive_release_on_mesh(*, centerline_utm: Any, mesh: Any,
                           fraction: float) -> tuple[tuple[float, float], str | None]:
    """A DERIVED release: ``fraction`` along the centerline, inside the mesh.

    The centerline is the whole navigated stretch; the accepted mesh is only the
    part of it the mapped banks and the cleanup left, so a station on the line is
    not a station in the domain. A source the solver cannot find an element for
    stops the run at startup with nothing but "SOURCE POINT OUTSIDE DOMAIN", so
    the station is walked DOWNSTREAM to the first one the triangulation actually
    holds and the distance it travelled is said out loud.

    Returns ``((lon, lat), note)`` in EPSG:4326; the note is ``None`` when the
    declared station was already inside.
    """
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
                logger.info("derived release walked %.0f m downstream into the "
                            "meshed reach", walked)
            return (float(lon), float(lat)), note
        walked += step
    raise TelemacDyeScenarioError(
        "TELEMAC_DYE_SCENARIO_ERROR",
        f"no point on the centerline at or below {frac:.0%} of its length lies "
        "inside the accepted mesh, so there is nowhere in the solved domain to "
        "put the release. Mesh more of the reach (a finer mesh_resolution_m or a "
        "supplied mesh) or place the release explicitly.")


def snap_release_to_wetted(point_utm: tuple[float, float], *, node_xy: Any,
                           wet: Any, state: str) -> tuple[tuple[float, float],
                                                          float, int]:
    """Put a release where the run holds WATER at t0 -> where it went, how far.

    The engine solves a source at a mesh NODE - ``proxim.f`` picks the nearest
    vertex of the element the coordinates fall in - so the node nearest the
    settled point is where the substance actually enters, and whether THAT node
    is wet when the run opens is the question the domain tests upstream never
    ask. A dry one discharges the substance into ground: the plume then starts
    wherever the water later arrives rather than where it was released. So a dry
    landing moves to the nearest node the initial state does hold water at, and a
    wet one is left exactly where the user or the centerline put it.

    ``wet`` is the initial state's own wet mask over the same node numbering and
    ``state`` is what the deck says that state IS. A state with NO wet node
    anywhere refuses: there is nowhere in this run for a release to be.

    Returns ``((x, y), moved_m, node)`` in the mesh's own metres.
    """
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    mask = np.asarray(wet, dtype=bool)
    here = np.asarray(point_utm, dtype=float)
    reach = np.hypot(xy[:, 0] - here[0], xy[:, 1] - here[1])
    nearest = int(np.argmin(reach))
    if mask[nearest]:
        return (float(here[0]), float(here[1])), 0.0, nearest
    if not mask.any():
        raise TelemacDyeScenarioError(
            "TELEMAC_RELEASE_NOWHERE_WET",
            f"the release lands at mesh node {nearest}, {reach[nearest]:.0f} m "
            f"away and DRY, and there is no wet water to move it to: {state}. A "
            "release needs water at t0 - continue from a state that holds some, "
            "or run the scenario that fills the domain first.")
    wet_nodes = np.flatnonzero(mask)
    node = int(wet_nodes[np.argmin(reach[wet_nodes])])
    moved = float(np.hypot(xy[node, 0] - here[0], xy[node, 1] - here[1]))
    logger.info("release node %d is dry at t0; moved %.1f m to wet node %d",
                nearest, moved, node)
    return (float(xy[node, 0]), float(xy[node, 1])), moved, node


def contain_release_point(*, point: tuple[float, float], domain: Any,
                          flowline: Any) -> ContainedRelease:
    """Refuse a release outside ``domain``; snap one inside it onto ``flowline``.

    ``domain`` and ``flowline`` are geometry sources - inline GeoJSON, an
    object-store uri, or a path - read as they are, in EPSG:4326. Distances are
    measured in the domain's own UTM zone, because a snap reported in degrees is
    not a distance.
    """
    from shapely.geometry import Point, shape
    from shapely.ops import nearest_points, transform as _transform, unary_union
    from pyproj import Transformer

    from trid3nt_server.workflows.mesh.inputs import op_geometry
    from trid3nt_server.tools.processing._geometry_common import utm_epsg_for

    lon, lat = float(point[0]), float(point[1])
    polygons = _geometries(op_geometry(domain), ("Polygon", "MultiPolygon"))
    if not polygons:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the domain {domain!r} carries no polygon, so there is no shape a "
            "release point could be inside of.")
    lines = _geometries(op_geometry(flowline), ("LineString", "MultiLineString"))
    if not lines:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the flowline {flowline!r} carries no line, so there is no river to "
            "put the release on.")

    domain_ll = unary_union([shape(g) for g in polygons]).buffer(0)
    river_ll = unary_union([shape(g) for g in lines])
    epsg = utm_epsg_for(float(domain_ll.centroid.x), float(domain_ll.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    domain_m = _transform(forward.transform, domain_ll)
    here = Point(*forward.transform(lon, lat))

    if not domain_m.covers(here):
        raise TelemacReleaseOutsideDomainError(
            lon, lat, float(here.distance(domain_m)))

    # The flowline is clipped to the domain FIRST: a river that runs on past the
    # modeled stretch would otherwise offer its own out-of-domain length as the
    # nearest point, and the run would solve a source it just refused to accept.
    reach_m = _transform(forward.transform, river_ll).intersection(domain_m)
    if reach_m.is_empty:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the flowline {flowline!r} does not run through the domain "
            f"{domain!r}, so the two describe different reaches and there is no "
            "river inside the domain to place the release on.")

    snapped_m, _ = nearest_points(reach_m, here)
    distance = float(here.distance(snapped_m))
    snapped_lon, snapped_lat = back.transform(snapped_m.x, snapped_m.y)
    logger.info("release point (%.5f, %.5f) contained; snapped %.1f m to "
                "(%.5f, %.5f)", lon, lat, distance, snapped_lon, snapped_lat)
    return ContainedRelease(lon=float(snapped_lon), lat=float(snapped_lat),
                            snap_distance_m=distance)


def _geometries(doc: Any, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every geometry of one of ``kinds`` in a GeoJSON document, at any depth."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        kind = str(node.get("type") or "")
        if kind == "FeatureCollection":
            for feature in node.get("features") or ():
                walk(feature)
        elif kind == "Feature":
            walk(node.get("geometry"))
        elif kind == "GeometryCollection":
            for part in node.get("geometries") or ():
                walk(part)
        elif kind in kinds:
            out.append(dict(node))

    walk(doc)
    return out
