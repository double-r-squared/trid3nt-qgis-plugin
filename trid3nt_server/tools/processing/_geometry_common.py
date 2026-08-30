"""Reading a GEOMETRY SOURCE, shared by the generic geometry primitives.

The local UTM zone lives here too, because measuring a shape in metres is what
every one of them does first and a second copy of the arithmetic is a second
chance for a zone to disagree with itself on a run that straddles a boundary.

One reader, because a chain hands the same thing to every link: a layer object a
producer returned, the uri that layer carries, a path on disk, or inline GeoJSON.
A tool that unwrapped only some of those would refuse a value the tool beside it
accepts, and a chain would then depend on which link it reached first.

Geometry TYPES are never inferred here. What comes back is the document as it was
written, flattened to its geometries; which of them a caller wants is the
caller's own question.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Mapping

__all__ = ["GeometryReadError", "flatten_geometries", "read_geometry_doc",
           "source_uri", "utm_epsg_for"]


class GeometryReadError(RuntimeError):
    """A typed geometry-read refusal: an error code plus what to supply instead."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def utm_epsg_for(lon: float, lat: float) -> int:
    """The WGS84 UTM zone EPSG a lon/lat falls in - THE one implementation.

    Clamped to the 60 real zones, so a longitude at or past the antimeridian
    reads the edge zone rather than a code no CRS registry carries.
    """
    zone = min(60, max(1, int((float(lon) + 180.0) // 6.0) + 1))
    return (32600 if float(lat) >= 0.0 else 32700) + zone


def source_uri(source: Any) -> Any:
    """The uri a layer/artifact value carries, or the value itself.

    A producer returns a ``LayerURI``, a declaration hands one straight on, and a
    person types a path. All three name the same file, so all three enter here.
    """
    uri = getattr(source, "uri", None)
    if uri is None and isinstance(source, Mapping):
        uri = source.get("uri")
    return source if uri is None else uri


def read_geometry_doc(source: Any) -> dict[str, Any]:
    """A geometry source -> GeoJSON, whatever vector format it arrived in."""
    from trid3nt_server.tools.processing._hydrology_common import _stage_uri_local

    resolved = source_uri(source)
    if isinstance(resolved, Mapping):
        return dict(resolved)
    text = str(resolved).strip()
    if text.startswith("{"):
        return json.loads(text)
    if text.lower().endswith((".geojson", ".json")):
        with tempfile.TemporaryDirectory(prefix="trid3nt_geom_") as tmpdir:
            with open(_stage_uri_local(text, tmpdir, "geometry"),
                      encoding="utf-8") as handle:
                return json.load(handle)
    if not (text.startswith("s3://") or os.path.exists(text)):
        raise GeometryReadError(
            "GEOMETRY_SOURCE_UNREADABLE",
            f"the geometry {source!r} could not be read: it is neither inline "
            "GeoJSON, an object-store uri, nor a file on disk.")
    import geopandas as gpd

    with tempfile.TemporaryDirectory(prefix="trid3nt_geom_") as tmpdir:
        path = _stage_uri_local(text, tmpdir, "geometry")
        return json.loads(gpd.read_file(path).to_crs(4326).to_json())


def flatten_geometries(doc: Any) -> list[dict[str, Any]]:
    """A GeoJSON document -> its geometries, collections walked through.

    Features lose their properties on the way out: these primitives combine and
    measure SHAPES, and carrying a source layer's attribute table into a document
    that mixes two of them would put two schemas under one set of column names.
    """
    out: list[dict[str, Any]] = []

    def walk(geometry: Any) -> None:
        if not isinstance(geometry, Mapping):
            return
        kind = str(geometry.get("type") or "")
        if kind == "GeometryCollection":
            for part in geometry.get("geometries") or ():
                walk(part)
        elif kind == "Feature":
            walk(geometry.get("geometry"))
        elif kind == "FeatureCollection":
            for feature in geometry.get("features") or ():
                walk(feature)
        elif kind:
            out.append(dict(geometry))

    walk(doc if isinstance(doc, Mapping) else None)
    return out
