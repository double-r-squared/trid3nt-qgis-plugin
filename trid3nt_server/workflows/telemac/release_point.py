"""Where a release is allowed to be: inside the domain, on the river.

A release point is a claim about where the substance enters the water, and two
things make it a solvable claim - it has to lie inside the DOMAIN the run is
solved over, and it has to sit ON the river rather than beside it. Both are
answered here, on the server, before anything is staged: the domain polygon and
the flowline are real fetched geometry the run already holds, so a point that
cannot be honored is refused while the user can still move it, and a point that
can is moved onto the flowline by the shortest step there is.

Nothing here invents a tolerance. A band around the flowline would be a second,
softer domain standing in for the one the run actually has, and a point it
admitted would be a release inside a shape nobody mapped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trid3nt_server.workflows.telemac.steps.errors import (
    TelemacDyeScenarioError,
    TelemacReleaseOutsideDomainError,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.release_point")

__all__ = ["ContainedRelease", "contain_release_point", "domain_polygon_of"]


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


def domain_polygon_of(artifact: Any) -> Any | None:
    """The polygon the accepted mesh was cut from, or ``None`` for a box domain.

    The mesh records the spec it was built from, so the domain a containment test
    has to be against is the mesh's own statement of it rather than a second
    resolution of the same question. A bbox extent is four numbers and no
    polygon: there is nothing here to be inside of, and this says so by
    answering None.
    """
    spec = ((getattr(artifact, "provenance", None) or {}).get("spec") or {})
    extent = spec.get("extent")
    if isinstance(extent, (tuple, list)) or extent is None:
        return None
    return extent


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

    from trid3nt_server.workflows.mesh.meshers.om2d import read_geometry
    from trid3nt_server.workflows.mesh.watershed import utm_epsg_for

    lon, lat = float(point[0]), float(point[1])
    polygons = _geometries(read_geometry(domain), ("Polygon", "MultiPolygon"))
    if not polygons:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the domain {domain!r} carries no polygon, so there is no shape a "
            "release point could be inside of.")
    lines = _geometries(read_geometry(flowline), ("LineString", "MultiLineString"))
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
