"""A STRUCTURE: a built thing in the water, ingested as the footprint it occupies.

A survey maps a breakwater, a groyne or a wall as a CENTRELINE, and a centreline
bounds no area: subtracting it removes nothing and a triangulation closes
straight over it. The footprint is that centreline given its DECLARED width. A
structure already mapped as a polygon is its own footprint and is used verbatim.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from .shape import polygons, polylines, shape

__all__ = ["StructureError", "structure", "transect"]

logger = logging.getLogger("trid3nt_server.inputs.structure")

_CODE = "STRUCTURE_INVALID"


class StructureError(RuntimeError):
    """A typed refusal: no structure was given, or its width is not a length."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def structure(value: Any, *, width_m: Any, asked: str = "the structure",
              label: str = "structure", code: str = _CODE) -> dict[str, Any]:
    """THE ingestion: a drawn line, a mapped structure or a polygon -> a footprint.

    Returned inline as GeoJSON in EPSG:4326, which is what a mesh op stages; a
    line with no width, and a slot with no structure at all, refuse typed."""
    drawn = shape(value, label=label, code=code)
    if drawn is None:
        raise StructureError(
            code,
            f"{asked} is the question and cannot be left out. Hand the slot a "
            "structure layer or a drawn line.")
    mapped = polygons(drawn)
    if mapped:
        logger.info("%s: %d mapped polygon(s) used as their own footprint",
                    label, len(mapped))
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": geometry}
            for geometry in mapped]}
    lines = polylines(drawn, label=label, code=code)
    try:
        width = float(width_m)
    except (TypeError, ValueError):
        width = 0.0
    if width <= 0.0:
        raise StructureError(
            code,
            f"{asked} is mapped as a line, which bounds no area, so it cannot "
            f"remove water until it is given a width; got width_m={width_m!r}.")
    return _buffered(lines, width, label)


def _buffered(lines: list[Any], width_m: float, label: str) -> dict[str, Any]:
    """The centrelines widened to their footprint, in the 4326 a recipe speaks."""
    import geopandas as gpd
    from shapely.geometry import LineString

    series = gpd.GeoSeries([LineString(line) for line in lines], crs=4326)
    metric = series.estimate_utm_crs()
    footprint = series.to_crs(metric).buffer(width_m / 2.0).union_all()
    logger.info("%s: %d line(s) widened to %g m", label, len(lines), width_m)
    return json.loads(gpd.GeoSeries([footprint], crs=metric).to_crs(4326).to_json())


#: The two ways a direction is stated: a compass bearing clockwise from north, or
#: a trigonometric angle counter-clockwise from east, which is the convention a
#: wave propagation direction is written in.
_CONVENTIONS = ("compass", "trig")


def transect(value: Any, *, bearing_deg: Any, length_m: Any,
             convention: str = "trig", label: str = "transect",
             code: str = _CODE) -> dict[str, Any]:
    """THE LINE a reading across a structure is taken along: through the
    structure's centroid, along a stated direction, half its length each way.

    A read that runs ACROSS a built thing has no line of its own - the domain's
    centerline runs down the water, not through the breakwater - so the line is
    measured off the structure the question is about. Laid in the structure's own
    UTM zone, so the length is metres on the ground, and returned as the one
    GeoJSON geometry every placed read is measured along.
    """
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    from .geometry import flatten_geometries, utm_epsg_for

    drawn = shape(value, label=label, code=code)
    geometries = [] if drawn is None else [
        _shape(g) for g in flatten_geometries(drawn.features)]
    geometries = [g for g in geometries if g is not None and not g.is_empty]
    if not geometries:
        raise StructureError(
            code, f"the structure {value!r} carries no geometry, so there is "
            "nothing to centre a transect on.")
    if convention not in _CONVENTIONS:
        raise StructureError(
            code, f"convention must be one of {list(_CONVENTIONS)}; got "
            f"{convention!r}.")
    try:
        length = float(length_m)
    except (TypeError, ValueError):
        length = 0.0
    if length <= 0.0:
        raise StructureError(
            code, f"the transect is a length on the ground; got "
            f"length_m={length_m!r}.")
    angle = math.radians(float(bearing_deg))
    east, north = ((math.sin(angle), math.cos(angle)) if convention == "compass"
                   else (math.cos(angle), math.sin(angle)))
    centre = unary_union(geometries).centroid
    epsg = utm_epsg_for(float(centre.x), float(centre.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    cx, cy = forward.transform(float(centre.x), float(centre.y))
    half = length / 2.0
    ends = [back.transform(cx - half * east, cy - half * north),
            back.transform(cx + half * east, cy + half * north)]
    logger.info("%s: %.0f m through (%.5f, %.5f) along %g deg %s in EPSG:%d",
                label, length, centre.x, centre.y, float(bearing_deg),
                convention, epsg)
    return {"type": "LineString",
            "coordinates": [[float(x), float(y)] for x, y in ends]}
