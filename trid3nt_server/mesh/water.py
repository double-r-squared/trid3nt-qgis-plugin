"""The water a mapped coastline leaves inside a box - the polygon a mesh is cut from.

A coastline is a LINE, and a line is not a domain. OpenStreetMap draws one with
the land on the LEFT of the way's direction, and that convention is the whole of
the classification: the box and the ways split the extent into faces, and each
way says which of the two faces it divides is land. What is left is the water.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Sequence

from trid3nt_server.mesh.meshers import MeshToolError

__all__ = ["water_polygon"]

logger = logging.getLogger(__name__)

#: How far off a coastline segment the land/water probe stands, as a fraction of
#: the box's shorter span. Small enough to stay inside the face the segment
#: bounds, large enough to survive the coordinate's own precision.
_PROBE_FRAC = 1.0e-4


def _geometries(source: Any) -> list[dict[str, Any]]:
    """Every geometry a layer, a path or inline GeoJSON carries."""
    from trid3nt_server.inputs.geometry import flatten_geometries, read_geometry_doc

    return [dict(g) for g in flatten_geometries(read_geometry_doc(source))]


def _box(extent: Any) -> tuple[float, float, float, float]:
    """The lon/lat box a caller stated - four numbers, or a shape's own bounds."""
    if isinstance(extent, (list, tuple)) and len(extent) == 4 \
            and all(isinstance(v, (int, float)) for v in extent):
        west, south, east, north = (float(v) for v in extent)
    else:
        from shapely.geometry import shape as _shape
        from shapely.ops import unary_union

        shapes = [_shape(g) for g in _geometries(extent)]
        if not shapes:
            raise MeshToolError(
                "MESH_WATER_NO_BOX",
                f"the extent {extent!r} is neither a (min_lon, min_lat, max_lon, "
                "max_lat) box nor a shape with bounds, so there is nothing for "
                "the coastline to cut.")
        west, south, east, north = unary_union(shapes).bounds
    if not (east > west and north > south):
        raise MeshToolError(
            "MESH_WATER_NO_BOX",
            f"the extent spans {(west, south, east, north)}, which encloses no "
            "area; state a box with width and height.")
    return (float(west), float(south), float(east), float(north))


def _walks(coastline: Any) -> list[list[tuple[float, float]]]:
    """The coastline as coordinate walks, IN THE DIRECTION IT WAS DRAWN.

    The direction is the datum here, so a reader that reorders the vertices would
    swap land for water."""
    walks: list[list[tuple[float, float]]] = []
    for geometry in _geometries(coastline):
        kind = str(geometry.get("type") or "")
        if kind == "LineString":
            walks.append([(float(x), float(y))
                          for x, y in geometry["coordinates"]])
        elif kind == "MultiLineString":
            for part in geometry["coordinates"]:
                walks.append([(float(x), float(y)) for x, y in part])
    return [walk for walk in walks if len(walk) >= 2]


def _inside(walk: Sequence[tuple[float, float]],
            frame: Any) -> list[list[tuple[float, float]]]:
    """The parts of one walk inside the box, each in the walk's own direction."""
    from shapely.geometry import LineString

    cut = LineString(walk).intersection(frame)
    parts: list[list[tuple[float, float]]] = []

    def take(geom: Any) -> None:
        kind = geom.geom_type
        if kind == "LineString" and len(geom.coords) >= 2:
            parts.append([(float(x), float(y)) for x, y in geom.coords])
        elif kind in ("MultiLineString", "GeometryCollection"):
            for part in geom.geoms:
                take(part)

    if not getattr(cut, "is_empty", True):
        take(cut)
    return parts


def _sides(walks: Sequence[Sequence[tuple[float, float]]],
           bbox: tuple[float, float, float, float]) -> tuple[list[Any], list[Any]]:
    """The faces the coastline splits the box into, as ``(land, water)``.

    A walk that ENDS inside the box divides nothing: refused, never guessed."""
    from shapely.geometry import LineString, Point, box
    from shapely.ops import polygonize, unary_union

    frame = box(*bbox)
    clipped = [part for walk in walks for part in _inside(walk, frame)]
    if not clipped:
        raise MeshToolError(
            "MESH_WATER_NO_COASTLINE",
            f"no coastline way crosses {bbox}, so nothing here divides land from "
            "water. An inland water body is natural=water rather than a "
            "coastline: fetch its polygon instead of cutting one.")
    faces = list(polygonize(unary_union(
        [frame.exterior, *(LineString(part) for part in clipped)])))
    step = _PROBE_FRAC * min(bbox[2] - bbox[0], bbox[3] - bbox[1])
    on: tuple[set[int], set[int]] = (set(), set())
    for part in clipped:
        for (x0, y0), (x1, y1) in zip(part[:-1], part[1:]):
            span = math.hypot(x1 - x0, y1 - y0)
            if span == 0.0:
                continue
            mid = (0.5 * (x0 + x1), 0.5 * (y0 + y1))
            left = (-(y1 - y0) / span, (x1 - x0) / span)
            for index, sign in ((0, 1.0), (1, -1.0)):
                probe = Point(mid[0] + sign * step * left[0],
                              mid[1] + sign * step * left[1])
                for face, geom in enumerate(faces):
                    if geom.contains(probe):
                        on[index].add(face)
                        break
    both = on[0] & on[1]
    if both:
        raise MeshToolError(
            "MESH_WATER_DOES_NOT_CLOSE",
            f"{len(both)} of the {len(faces)} faces this box splits into are on "
            "BOTH sides of the coastline, so the coastline does not divide the "
            "box into land and water: a way ends inside it rather than crossing "
            "it. Widen the box so every coastline way crosses it, or supply the "
            "water body's own polygon.")
    return ([faces[i] for i in sorted(on[0])],
            [faces[i] for i in sorted(on[1])])


def water_polygon(coastline: Any, extent: Any) -> dict[str, Any]:
    """The water inside ``extent`` that ``coastline`` leaves, as one GeoJSON geometry.

    ``coastline`` is a line layer, a uri or inline GeoJSON whose ways are read in
    the direction they were drawn; ``extent`` is a lon/lat box or any shape whose
    bounds are that box. A way that ends inside the box divides nothing and
    refuses; a box with no water in it refuses too."""
    from shapely.geometry import mapping
    from shapely.ops import unary_union

    bbox = _box(extent)
    walks = _walks(coastline)
    if not walks:
        raise MeshToolError(
            "MESH_WATER_NO_COASTLINE",
            f"the coastline {coastline!r} carries no line geometry, so there is "
            "nothing to cut the box with.")
    land, water = _sides(walks, bbox)
    if not water:
        raise MeshToolError(
            "MESH_WATER_ALL_LAND",
            f"the coastline over {bbox} leaves no water inside it: every face "
            f"the box splits into is on the land side of {len(walks)} way(s). "
            "Move the box onto the water, or widen it past the shore.")
    merged = unary_union(water)
    fraction = float(merged.area / ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])))
    logger.info("mesh water: %d way(s) over %s -> %d water part(s), %.1f%% of "
                "the box (%d land face(s))", len(walks), bbox,
                len(getattr(merged, "geoms", [merged])), fraction * 100.0,
                len(land))
    return mapping(merged)
