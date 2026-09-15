"""A STRUCTURE: a built thing in the water, ingested as the footprint it occupies.

A survey maps a breakwater, a groyne or a wall as a CENTRELINE, and a centreline
bounds no area: subtracting it removes nothing and a triangulation closes
straight over it. The footprint is that centreline given its DECLARED width. A
structure already mapped as a polygon is its own footprint and is used verbatim.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .shape import polygons, polylines, shape

__all__ = ["StructureError", "structure"]

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
