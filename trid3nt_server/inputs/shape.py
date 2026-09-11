"""A SHAPE: a feature collection with an optional name, from the draw or a layer.

The role-tagged collection a draw returns, a stored vector layer, a bare GeoJSON
geometry and a typed vertex list all enter through ``shape``; what reads a Shape
after that asks it for its ``polylines`` or its ``polygons`` in lon/lat and never
parses again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_server.workflows.runtime.user_input import UserInputError, polyline_set

from .geometry import flatten_geometries, read_geometry_doc

__all__ = ["Shape", "polygons", "polylines", "shape"]

logger = logging.getLogger("trid3nt_server.inputs.shape")

_CODE = "SHAPE_INVALID"
_LAYER_SCHEMES = ("s3://", "gs://", "file://", "/", "./")


@dataclass(frozen=True, slots=True)
class Shape:
    """One GeoJSON FeatureCollection in EPSG:4326, and the name the user gave it."""

    features: dict[str, Any]
    name: str | None = None


def shape(value: Any, *, label: str = "shape", code: str = _CODE) -> Shape | None:
    """THE ingestion: a drawn collection, a layer, a geometry or vertices -> Shape.

    ``None`` only when nothing came; a value of no readable shape refuses typed."""
    if value is None or isinstance(value, Shape):
        return value
    if isinstance(value, Mapping):
        return Shape(_collection(value), _named(value))
    uri = getattr(value, "uri", None) or (value if isinstance(value, str) else None)
    if isinstance(uri, str) and uri.strip().startswith(_LAYER_SCHEMES):
        doc = read_geometry_doc(uri.strip())
        logger.info("%s: read from %s", label, uri)
        return Shape(_collection(doc))
    if isinstance(value, str) and value.strip().startswith("{"):
        return shape(json.loads(value), label=label, code=code)
    lines = polyline_set(value, label=label, code=code)
    return Shape({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString", "coordinates": line}}
        for line in lines or ()]})


def _named(value: Mapping[str, Any]) -> str | None:
    name = value.get("name")
    text = str(name).strip() if name is not None else ""
    return text or None


def _collection(doc: Mapping[str, Any]) -> dict[str, Any]:
    kind = doc.get("type")
    if kind == "FeatureCollection":
        return dict(doc)
    if kind == "Feature":
        return {"type": "FeatureCollection", "features": [dict(doc)]}
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": dict(doc)}]}


def polylines(shp: Shape, *, label: str = "shape",
              code: str = _CODE) -> list[list[list[float]]]:
    """Every line the shape carries as ``[[lon, lat], ...]`` lists; none refuses."""
    out: list[list[list[float]]] = []
    for geometry in flatten_geometries(shp.features):
        kind = geometry.get("type")
        parts = (geometry.get("coordinates") if kind == "MultiLineString"
                 else [geometry.get("coordinates")] if kind == "LineString" else ())
        for part in parts:
            coords = [[float(x), float(y)] for x, y in part]
            if len(coords) >= 2:
                out.append(coords)
    if not out:
        raise UserInputError(
            f"the {label} carries no line geometry. Supply a line layer, sketch "
            "one, or omit the slot.", code=code)
    return out


def polygons(shp: Shape) -> list[dict[str, Any]]:
    """Every polygon geometry the shape carries, as GeoJSON."""
    return [g for g in flatten_geometries(shp.features)
            if g.get("type") in ("Polygon", "MultiPolygon")]
