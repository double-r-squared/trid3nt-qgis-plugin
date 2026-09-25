"""Reading a GEOMETRY SOURCE, shared by the generic geometry primitives.

ONE reader for a layer object, the uri it carries, a path on disk or inline GeoJSON.
Geometry TYPES are never inferred: the document comes back flattened to its geometries.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Mapping

__all__ = ["GeometryReadError", "covered_fraction", "flatten_geometries",
           "read_geometry_doc", "source_uri", "utm_epsg_for"]


class GeometryReadError(RuntimeError):
    """A typed geometry-read refusal: an error code plus what to supply instead."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def utm_epsg_for(lon: float, lat: float) -> int:
    """The WGS84 UTM zone EPSG a lon/lat falls in - THE one implementation.
    Clamped to the 60 real zones, so a longitude at or past the antimeridian reads
    the edge zone rather than a code no CRS registry carries."""
    zone = min(60, max(1, int((float(lon) + 180.0) // 6.0) + 1))
    return (32600 if float(lat) >= 0.0 else 32700) + zone


def source_uri(source: Any) -> Any:
    """The uri a layer/artifact value carries, or the value itself.
    A model with a ``uri``, a mapping carrying one, and a bare path all name the
    same file, so all three enter here."""
    uri = getattr(source, "uri", None)
    if uri is None and isinstance(source, Mapping):
        uri = source.get("uri")
    return source if uri is None else uri


def read_geometry_doc(source: Any) -> dict[str, Any]:
    """A geometry source -> GeoJSON, whatever vector format it arrived in."""
    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

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
    Features LOSE their properties: a document mixing two sources would otherwise
    carry two attribute schemas under one set of column names."""
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


#: The TIN suffixes a built mesh's display face arrives under. Anything else is
#: read as a vector document of polygons.
_MESH_SUFFIXES = (".2dm",)


def _covering_line(source: Any) -> Any:
    """The one line a coverage is measured along, in EPSG:4326."""
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    parts = [_shape(g) for g in flatten_geometries(read_geometry_doc(source))
             if str(g.get("type") or "") in ("LineString", "MultiLineString")]
    if not parts:
        raise GeometryReadError(
            "COVERAGE_NO_LINE",
            f"the layer {source!r} carries no polyline, so there is no line to "
            "measure coverage along.")
    return unary_union(parts)


def _in_polygons(line_4326: Any, source: Any) -> tuple[float, float, int]:
    """``(covered_m, length_m, epsg)`` of the line inside the polygons handed in."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    polys = [_shape(g).buffer(0) for g in flatten_geometries(read_geometry_doc(source))
             if str(g.get("type") or "") in ("Polygon", "MultiPolygon")]
    if not polys:
        raise GeometryReadError(
            "COVERAGE_NO_AREA",
            f"the layer {source!r} carries no polygon, so there is no area the "
            "line could lie inside.")
    epsg = utm_epsg_for(float(line_4326.centroid.x), float(line_4326.centroid.y))
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True).transform
    line_m = _transform(to_utm, line_4326)
    area_m = _transform(to_utm, unary_union(polys))
    if line_m.length <= 0.0:
        return (0.0, 0.0, epsg)
    return (float(line_m.intersection(area_m).length), float(line_m.length), epsg)


def _in_mesh_cells(line: Any, uri: str, epsg: int | None) -> tuple[float, float, int]:
    """``(covered_m, length_m, epsg)`` of the line inside a mesh's own cells.

    Summed over the cells the line touches: a triangulation tiles its domain
    without overlap, so the per-cell lengths already add to the length inside it
    and no union of tens of thousands of triangles has to be built."""
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from trid3nt_server.mesh.shared.nodes import (
        read_accepted_mesh_nodes, read_centerline_utm,
    )

    if not epsg:
        raise GeometryReadError(
            "COVERAGE_UNPROJECTED",
            f"the mesh face {uri!r} carries its nodes in metres but names no "
            "projected zone, so the line cannot be placed on it.")
    points_utm, cells, _bed, _lonlat = read_accepted_mesh_nodes(uri, utm_epsg=None)
    line_m = LineString(read_centerline_utm(line, int(epsg)))
    if line_m.length <= 0.0:
        return (0.0, 0.0, int(epsg))
    rings = np.asarray(points_utm, dtype=float)[np.asarray(cells, dtype=np.int64)]
    cell_polygons = shapely.polygons(np.concatenate([rings, rings[:, :1]], axis=1))
    touched = shapely.STRtree(cell_polygons).query(line_m, predicate="intersects")
    covered = sum(float(line_m.intersection(cell_polygons[i]).length) for i in touched)
    return (min(covered, float(line_m.length)), float(line_m.length), int(epsg))


def covered_fraction(line: Any, within: Any,
                     within_epsg: int | None = None) -> dict[str, Any]:
    """How much of a LINE lies inside an area, as a fraction, plus its note.

    The area is polygons (what a water-body source maps) or the cells of a built
    mesh (what a triangulation actually holds), read off the file it arrived in.
    Lengths are metres in the line's own UTM zone, never in degree space; a line
    and an area that do not meet at all is a fact about the two inputs, so it is
    reported as zero rather than measured further."""
    line_4326 = _covering_line(line)
    uri = str(source_uri(within) or "")
    if uri.lower().endswith(_MESH_SUFFIXES):
        covered, length, epsg = _in_mesh_cells(line, uri, within_epsg)
        kind = "mesh cells"
        remedy = (" A finer resolution, a declared sizing function or a supplied "
                  "mesh is how more of it gets held.")
    else:
        covered, length, epsg = _in_polygons(line_4326, within)
        kind = "polygons"
        remedy = ""
    fraction = (covered / length) if length > 0.0 else 0.0
    note = (f"coverage: {fraction:.1%} of the {length / 1000.0:.2f} km line lies "
            f"inside the {kind} (measured in EPSG:{epsg}). Anything outside them "
            "carries no data from this source and is not what the answer is "
            f"about.{remedy}")
    return {"fraction": round(fraction, 6), "covered_m": round(covered, 2),
            "length_m": round(length, 2), "of": kind, "epsg": epsg, "note": note}
